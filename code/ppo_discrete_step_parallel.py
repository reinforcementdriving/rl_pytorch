
import pickle
from collections import deque
from copy import deepcopy

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.multiprocessing as mp
from torch.distributions import Categorical
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter
from running_mean_std import RunningMeanStd

N_PROCESS = 8
ROLL_LEN = 4096
BATCH_SIZE = 64 * N_PROCESS
LR = 0.00030
EPOCHS = 10
CLIP = 0.2
GAMMA = 0.99
LAMBDA = 0.95
ENT_COEF = 0.0
V_COEF = 0.5
V_CLIP = True
OBS_NORM = True
REW_NORM = True
GRAD_NORM = False
LIN_REDUCE = False

# set device
use_cuda = torch.cuda.is_available()
print('cuda:', use_cuda)
device = torch.device('cuda:0' if use_cuda else 'cpu')
writer = SummaryWriter()

# random seed
torch.manual_seed(0)
if use_cuda:
    torch.cuda.manual_seed_all(0)

# make an environment
# env = gym.make('CartPole-v0')
# env = gym.make('CartPole-v1')
env = gym.make('MountainCar-v0')
# env = gym.make('LunarLander-v2')


class ActorCriticNet(nn.Module):
    def __init__(self, obs_space, action_space):
        super().__init__()
        h = 64
        self.head = nn.Sequential(
            nn.Linear(obs_space, h),
            nn.Tanh()
        )
        self.pol = nn.Sequential(
            nn.Tanh(),
            nn.Linear(h, action_space)
        )
        self.val = nn.Sequential(
            nn.Tanh(),
            nn.Linear(h, 1)
        )
        self.log_softmax = nn.LogSoftmax(dim=-1)

    def forward(self, x):
        out = self.head(x)
        logit = self.pol(out).reshape(out.shape[0], -1)
        log_p = self.log_softmax(logit)
        v = self.val(out).reshape(out.shape[0], 1)

        return log_p, v


def learn(net, train_memory):
    global CLIP, LR
    global total_update, update, epochs
    old_net = deepcopy(net)
    net.train()
    old_net.train()

    if LIN_REDUCE:
        LR = LR - (LR * update / total_update)
        CLIP = CLIP - (CLIP * update / total_update)

    optimizer = torch.optim.Adam(net.parameters(), lr=LR, eps=1e-5)

    for _ in range(EPOCHS):
        losses = []
        epochs += 1
        dataloader = DataLoader(
            train_memory,
            shuffle=True,
            batch_size=BATCH_SIZE,
            pin_memory=use_cuda
        )
        for (s, a, ret, adv) in dataloader:
            s_batch = s.to(device).float()
            a_batch = a.to(device).long()
            ret_batch = ret.to(device).float()
            adv_batch = adv.to(device).float()
            adv_batch = (adv_batch - adv_batch.mean()) / adv_batch.std()
            with torch.no_grad():
                log_p_batch_old, v_batch_old = old_net(s_batch)
                log_p_acting_old = log_p_batch_old[range(BATCH_SIZE), a_batch]

            log_p_batch, v_batch = net(s_batch)
            log_p_acting = log_p_batch[range(BATCH_SIZE), a_batch]
            p_ratio = (log_p_acting - log_p_acting_old).exp()
            p_ratio_clip = torch.clamp(p_ratio, 1 - CLIP, 1 + CLIP)
            p_loss = torch.min(p_ratio * adv_batch,
                               p_ratio_clip * adv_batch).mean()
            if V_CLIP:
                v_clip = v_batch_old + \
                    torch.clamp(v_batch - v_batch_old, -CLIP, CLIP)
                v_loss1 = (ret_batch - v_clip).pow(2)
                v_loss2 = (ret_batch - v_batch).pow(2)
                v_loss = torch.max(v_loss1, v_loss2).mean()
            else:
                v_loss = (ret_batch - v_batch).pow(2).mean()

            log_p, _ = net(s_batch)
            entropy = -(log_p.exp() * log_p).sum(dim=1).mean()

            # loss
            loss = -(p_loss - V_COEF * v_loss + ENT_COEF * entropy)
            losses.append(loss.item())

            optimizer.zero_grad()
            loss.backward()

            if GRAD_NORM:
                nn.utils.clip_grad_norm_(net.parameters(), max_norm=0.5)

            optimizer.step()
        writer.add_scalar('data/loss', np.mean(losses), epochs)
    train_memory.clear()

    return net.state_dict()


def get_action_and_value(obs, old_net):
    old_net.eval()
    with torch.no_grad():
        state = torch.tensor([obs]).to(device).float()
        log_p, v = old_net(state)
        m = Categorical(log_p.exp())
        action = m.sample()

    return action.item(), v.item()


def compute_adv_with_gae(rewards, values, roll_memory):
    rew = np.array(rewards, 'float')
    val = np.array(values[:-1], 'float')
    _val = np.array(values[1:], 'float')
    ret = rew + GAMMA * _val
    delta = ret - val
    gae_dt = np.array(
        [(GAMMA * LAMBDA)**(i) * dt for i, dt in enumerate(delta.tolist())],
        'float')
    for i, data in enumerate(roll_memory):
        data.append(ret[i])
        data.append(sum(gae_dt[i:] / (GAMMA * LAMBDA)**(i)))

    rewards.clear()
    values.clear()

    return roll_memory


def roll_out(env, length, rank, child):
    env.seed(rank)

    # hyperparameter
    roll_len = length

    # for play
    episodes = 0
    steps = 0
    ep_rewards = []

    # memories
    train_memory = []
    roll_memory = []
    rewards = []
    values = []
    obses, rews = [], []

    # recieve
    old_net, norm_obs, norm_rew = child.recv()

    # Play!
    while True:
        obs = env.reset()
        done = False
        ep_reward = 0
        while not done:
            # env.render()
            obses.append(obs)
            if OBS_NORM:
                obs_norm = np.clip((obs - norm_obs.mean) /
                                   np.sqrt(norm_obs.var), -10, 10)
                action, value = get_action_and_value(obs_norm, old_net)
            else:
                action, value = get_action_and_value(obs, old_net)

            # step
            _obs, reward, done, _ = env.step(action)

            # store
            values.append(value)
            rews.append(reward)

            if OBS_NORM:
                roll_memory.append([obs_norm, action])
            else:
                roll_memory.append([obs, action])

            if REW_NORM:
                rew_norm = np.clip((reward - norm_rew.mean) /
                                   np.sqrt(norm_rew.var), -10, 10)
                rewards.append(rew_norm)
            else:
                rewards.append(reward)

            obs = _obs
            steps += 1
            ep_reward += reward

            if done or steps % roll_len == 0:
                if done:
                    obses.append(_obs)
                    _value = 0.
                else:
                    if OBS_NORM:
                        _obs_norm = np.clip(
                            (_obs - norm_obs.mean) /
                            np.sqrt(norm_obs.var), -10, 10)
                        _, _value = get_action_and_value(_obs_norm, old_net)
                    else:
                        _, _value = get_action_and_value(_obs, old_net)

                values.append(_value)
                train_memory.extend(
                    compute_adv_with_gae(rewards, values, roll_memory)
                )
                roll_memory.clear()

            if steps % roll_len == 0:
                child.send(
                    ((train_memory, ep_rewards, obses, rews), 'train', rank)
                )
                train_memory.clear()
                ep_rewards.clear()
                obses.clear()
                rews.clear()
                state_dict, norm_obs, norm_rew = child.recv()
                old_net.load_state_dict(state_dict)

        if done:
            episodes += 1
            ep_rewards.append(ep_reward)
            print('{:3} Episode in {:5} steps, reward {:.2f} [Process-{}]'
                  ''.format(episodes, steps, ep_reward, rank))


if __name__ == '__main__':
    mp.set_start_method('spawn')
    obs_space = env.observation_space.shape[0]
    action_space = env.action_space.n
    n_eval = env.spec.trials
    total_steps = 10e7
    total_update = float(total_steps // ROLL_LEN // N_PROCESS)

    net = ActorCriticNet(obs_space, action_space).to(device)
    norm_obs = RunningMeanStd(shape=env.observation_space.shape)
    norm_rew = RunningMeanStd()

    jobs = []
    pipes = []
    trajectory = []
    obses, rews = [], []
    rewards = deque(maxlen=n_eval)
    epochs = 0
    update = 0

    for i in range(N_PROCESS):
        parent, child = mp.Pipe()
        p = mp.Process(target=roll_out,
                       args=(env, ROLL_LEN, i, child),
                       daemon=True)
        jobs.append(p)
        pipes.append(parent)

    for i in range(N_PROCESS):
        pipes[i].send((net, norm_obs, norm_rew))
        jobs[i].start()

    while True:
        for i in range(N_PROCESS):
            data, msg, rank = pipes[i].recv()
            if msg == 'train':
                traj, ep_rews, obs, rew = data
                trajectory.extend(traj)
                rewards.extend(ep_rews)
                obses.extend(obs)
                rews.extend(rew)

        if len(rewards) == n_eval:
            writer.add_scalar('data/reward', np.mean(rewards), update)
            if np.mean(rewards) >= env.spec.reward_threshold:
                print('\n{} is sloved! [{} Update]'.format(
                    env.spec.id, update))
                torch.save(net.state_dict(),
                           f'./test/saved_models/'
                           f'{env.spec.id}_up{update}_clear_model_ppo_st.pt')
                with open(f'./test/saved_models/'
                          f'{env.spec.id}_up{update}_clear_norm_obs.pkl',
                          'wb') as f:
                    pickle.dump(norm_obs, f,
                                pickle.HIGHEST_PROTOCOL)
                break

        if len(trajectory) == ROLL_LEN * N_PROCESS:
            state_dict = learn(net, trajectory)
            if OBS_NORM:
                norm_obs.update(np.array(obses))
            if REW_NORM:
                norm_rew.update(np.array(rews))
            obses.clear()
            rews.clear()
            update += 1
            print(f'Update: {update}')
            for i in range(N_PROCESS):
                pipes[i].send((state_dict, norm_obs, norm_rew))

    env.close()
