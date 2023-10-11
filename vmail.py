import argparse
import collections
import functools
import os
import pathlib
import sys
import warnings
import json
os.environ['MUJOCO_GL'] = 'egl'

import numpy as np
import ruamel.yaml as yaml

sys.path.append(str(pathlib.Path(__file__).parent))
import dmc2gym
import exploration as expl
import models
import networks
import tools
import wrappers

import torch
from torch import nn
from torch import distributions as torchd
to_np = lambda x: x.detach().cpu().numpy()


class VMAIL(nn.Module):

    def __init__(self, config, logger, dataset, exp_dataset):
        super(VMAIL, self).__init__()
        self._config = config
        self._logger = logger
        self._should_log = tools.Every(config.log_every)
        self._should_train = tools.Every(config.train_every)
        self._should_pretrain = tools.Once()
        self._should_reset = tools.Every(config.reset_every)
        self._should_expl = tools.Until(int(
                config.expl_until / config.action_repeat))
        self._metrics = {}
        self._step = count_steps(config.traindir)
        # Schedules.
        config.actor_entropy = (
                lambda x=config.actor_entropy: tools.schedule(x, self._step))
        config.actor_state_entropy = (
                lambda x=config.actor_state_entropy: tools.schedule(x, self._step))
        config.imag_gradient_mix = (
                lambda x=config.imag_gradient_mix: tools.schedule(x, self._step))
        self._dataset = dataset
        self._exp_dataset = exp_dataset
        self._wm = models.WorldModel(self._step, config)
        self._task_behavior = models.ImagBehavior(
                config, self._wm, config.behavior_stop_grad)
        reward = lambda f, s, a: self._wm.heads['reward'](f).mean
        self._expl_behavior = dict(
                greedy=lambda: self._task_behavior,
                random=lambda: expl.Random(config),
                plan2explore=lambda: expl.Plan2Explore(config, self._wm, reward),
        )[config.expl_behavior]()
        

    def forward(self, obs, reset, state=None, reward=None, training=True):
        step = self._step
        if self._should_reset(step):
            state = None
        if state is not None and reset.any():
            mask = 1 - reset
            for key in state[0].keys():
                for i in range(state[0][key].shape[0]):
                    state[0][key][i] *= mask[i]
            for i in range(len(state[1])):
                state[1][i] *= mask[i]
        if training and self._should_train(step):
            steps = (
                    self._config.pretrain if self._should_pretrain()
                    else self._config.train_steps)
            for _ in range(steps):
                self._train(next(self._dataset), next(self._exp_dataset))
            if self._should_log(step):
                for name, values in self._metrics.items():
                    self._logger.scalar(name, float(np.mean(values)))
                    self._metrics[name] = []
                openl = self._wm.video_pred(next(self._dataset))
                self._logger.video('train_openl', to_np(openl))
                self._logger.write(fps=True)

        policy_output, state = self._policy(obs, state, training)

        if training:
            self._step += len(reset)
            self._logger.step = self._config.action_repeat * self._step
        return policy_output, state

    def _policy(self, obs, state, training):
        if state is None:
            batch_size = len(obs['image'])
            latent = self._wm.dynamics.initial(len(obs['image']))
            action = torch.zeros((batch_size, self._config.num_actions)).to(self._config.device)
        else:
            latent, action = state
        embed = self._wm.encoder(self._wm.preprocess(obs))
        latent, _ = self._wm.dynamics.obs_step(
                latent, action, embed, self._config.collect_dyn_sample)
        if self._config.eval_state_mean:
            latent['stoch'] = latent['mean']
        feat = self._wm.dynamics.get_feat(latent)
        if not training:
            actor = self._task_behavior.actor(feat)
            action = actor.mode()
        elif self._should_expl(self._step):
            # print("explore behavior")
            actor = self._expl_behavior.actor(feat)
            action = actor.sample()
        else:
            actor = self._task_behavior.actor(feat)
            action = actor.sample()
        logprob = actor.log_prob(action)
        latent = {k: v.detach()    for k, v in latent.items()}
        action = action.detach()
        if self._config.actor_dist == 'onehot_gumble':
            action = torch.one_hot(torch.argmax(action, dim=-1), self._config.num_actions)
        action = self._exploration(action, training)
        policy_output = {'action': action, 'logprob': logprob}
        state = (latent, action)
        return policy_output, state

    def _exploration(self, action, training):
        amount = self._config.expl_amount if training else self._config.eval_noise # 0
        if amount == 0:
            return torch.clip(action, -1, 1)
        if 'onehot' in self._config.actor_dist:
            probs = amount / self._config.num_actions + (1 - amount) * action
            return tools.OneHotDist(probs=probs).sample()
        else:
            return torch.clip(torchd.normal.Normal(action, amount).sample(), -1, 1)
        raise NotImplementedError(self._config.action_noise)

    def _train(self, data, expert_data):
        metrics = {}
        post, context, mets = self._wm._train(data)
        metrics.update(mets)
        start = post
        if self._config.pred_discount:    # Last step could be terminal.
            start = {k: v[:, :-1] for k, v in post.items()}
            context = {k: v[:, :-1] for k, v in context.items()}
        # for vmail, here we use discriminator to generate reward
        # reward = lambda f, s, a: self._wm.heads['reward'](
        #         self._wm.dynamics.get_feat(s)).mode()
        reward = lambda f, s, a: self._task_behavior.discriminator(torch.cat([f, a], dim=-1).detach()).mode().detach()
        metrics.update(self._task_behavior._train(start, expert_data, reward)[-1])
        if self._config.expl_behavior != 'greedy':
            if self._config.pred_discount:
                data = {k: v[:, :-1] for k, v in data.items()}
            mets = self._expl_behavior.train(start, context, data)[-1]
            metrics.update({'expl_' + key: value for key, value in mets.items()})
        for name, value in metrics.items():
            if not name in self._metrics.keys():
                self._metrics[name] = [value]
            else:
                self._metrics[name].append(value)


def count_steps(folder):
    return sum(int(str(n).split('-')[-1][:-4]) - 1 for n in folder.glob('*.npz'))


def make_dataset(episodes, config):
    generator = tools.sample_episodes(
            episodes, config.batch_length, config.oversample_ends)
    dataset = tools.from_generator(generator, config.batch_size)
    return dataset


def make_env(config, logger, mode, train_eps, eval_eps, video_dir):
    suite, domain_task_distractor = config.task.split('_', 1)
    domain, task_distractor = domain_task_distractor.split('_', 1)
    task, distractor = task_distractor.split('_', 1)
    print(suite, domain, task, distractor)

    if '-driving' in distractor:
        img_source = 'video'
        total_frames = 1000
        resource_files = os.path.join(video_dir, '*.mp4')
    elif '-noise' in distractor:
        img_source = 'noise'
        total_frames = None
        resource_files = None
    elif '-none' in distractor:
        img_source = None
        total_frames = None
        resource_files = None
    else:
        raise NotImplementedError

    if suite == 'dmc':
        env = dmc2gym.make(
            domain_name=domain,
            task_name=task,
            resource_files=resource_files,
            img_source=img_source,
            total_frames=total_frames,
            seed=config.seed,
            visualize_reward=False,
            from_pixels=True,
            height=config.size[0],
            width=config.size[1],
            frame_skip=config.action_repeat
        )
        env = wrappers.DMC2GYMWrapper(env)
    env = wrappers.TimeLimit(env, config.time_limit)
    env = wrappers.SelectAction(env, key='action')
    if (mode == 'train') or (mode == 'eval'):
        callbacks = [functools.partial(
                process_episode, config, logger, mode, train_eps, eval_eps)]
        env = wrappers.CollectDataset(env, callbacks)
    env = wrappers.RewardObs(env)
    return env


def process_episode(config, logger, mode, train_eps, eval_eps, episode):
    directory = dict(train=config.traindir, eval=config.evaldir)[mode]
    cache = dict(train=train_eps, eval=eval_eps)[mode]
    filename = tools.save_episodes(directory, [episode])[0]
    length = len(episode['reward']) - 1
    score = float(episode['reward'].astype(np.float64).sum())
    video = episode['image']
    if mode == 'eval':
        cache.clear()
    if mode == 'train' and config.dataset_size:
        total = 0
        for key, ep in reversed(sorted(cache.items(), key=lambda x: x[0])):
            if total <= config.dataset_size - length:
                total += len(ep['reward']) - 1
            else:
                del cache[key]
        logger.scalar('dataset_size', total + length)
    cache[str(filename)] = episode
    print(f'{mode.title()} episode has {length} steps and return {score:.1f}.')
    logger.scalar(f'{mode}_return', score)
    logger.scalar(f'{mode}_length', length)
    logger.scalar(f'{mode}_episodes', len(cache))
    if mode == 'eval' or config.expl_gifs:
        logger.video(f'{mode}_policy', video[None])
    logger.write()


def main(config):
    logdir = os.path.join(config.logdir, config.task, 'vmail', 'seed-'+str(config.seed))
    logdir = pathlib.Path(logdir).expanduser()
    config.traindir = logdir / 'train_eps'
    config.evaldir = logdir / 'eval_eps'
    config.steps //= config.action_repeat
    config.eval_every //= config.action_repeat
    config.log_every //= config.action_repeat
    config.time_limit //= config.action_repeat
    config.act = getattr(torch.nn, config.act)
    
    print('Logdir', logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    config.traindir.mkdir(parents=True, exist_ok=True)
    config.evaldir.mkdir(parents=True, exist_ok=True)
    step = count_steps(config.traindir)
    logger = tools.Logger(logdir, config.action_repeat * step)

    print('Load expert data.')
    expert_eps = tools.load_episodes(config.expert_datadir)
    exp_dataset = make_dataset(expert_eps, config)

    print('Create envs.')
    if config.offline_traindir:
        directory = config.offline_traindir.format(**vars(config))
    else:
        directory = config.traindir
    train_eps = tools.load_episodes(directory, limit=config.dataset_size)
    if config.offline_evaldir:
        directory = config.offline_evaldir.format(**vars(config))
    else:
        directory = config.evaldir
    eval_eps = tools.load_episodes(directory, limit=1)
    make = lambda mode: make_env(config, logger, mode, train_eps, eval_eps, config.video_dir)
    train_envs = [make('train') for _ in range(config.envs)]
    eval_envs = [make('eval') for _ in range(config.envs)]
    acts = train_envs[0].action_space
    config.num_actions = acts.n if hasattr(acts, 'n') else acts.shape[0]
    # with open(os.path.join(logdir, 'args.json'), 'w') as f:
    #     json.dump(vars(configs), f, sort_keys=True, indent=4)


    if not config.offline_traindir:
        prefill = max(0, config.prefill - count_steps(config.traindir))
        print(f'Prefill dataset ({prefill} steps).')
        if hasattr(acts, 'discrete'):
            random_actor = tools.OneHotDist(torch.zeros_like(torch.Tensor(acts.low))[None])
        else:
            random_actor = torchd.independent.Independent(
                    torchd.uniform.Uniform(torch.Tensor(acts.low)[None],
                                                                 torch.Tensor(acts.high)[None]), 1)
        def random_agent(o, d, s, r):
            action = random_actor.sample()
            logprob = random_actor.log_prob(action)
            return {'action': action, 'logprob': logprob}, None
        tools.simulate(random_agent, train_envs, prefill)
        tools.simulate(random_agent, eval_envs, episodes=1)
        logger.step = config.action_repeat * count_steps(config.traindir)

    print('Simulate agent.')
    train_dataset = make_dataset(train_eps, config)
    eval_dataset = make_dataset(eval_eps, config)
    agent = VMAIL(config, logger, train_dataset, exp_dataset).to(config.device)
    agent.requires_grad_(requires_grad=False)
    if (logdir / 'latest_model.pt').exists():
        agent.load_state_dict(torch.load(logdir / 'latest_model.pt'))
        agent._should_pretrain._once = False

    state = None
    while agent._step < config.steps:
        logger.write()
        print('Start evaluation.')
        video_pred = agent._wm.video_pred(next(eval_dataset))
        logger.video('eval_openl', to_np(video_pred))
        eval_policy = functools.partial(agent, training=False)
        tools.simulate(eval_policy, eval_envs, episodes=1)
        print('Start training.')
        state = tools.simulate(agent, train_envs, config.eval_every, state=state)
        torch.save(agent.state_dict(), logdir / 'latest_model.pt')
    for env in train_envs + eval_envs:
        try:
            env.close()
        except Exception:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--configs', nargs='+', required=True)
    args, remaining = parser.parse_known_args()
    configs = yaml.safe_load(
            (pathlib.Path(sys.argv[0]).parent / 'configs.yaml').read_text())
    defaults = {}
    for name in args.configs:
        defaults.update(configs[name])
    parser = argparse.ArgumentParser()
    for key, value in sorted(defaults.items(), key=lambda x: x[0]):
        arg_type = tools.args_type(value)
        parser.add_argument(f'--{key}', type=arg_type, default=arg_type(value))
    main(parser.parse_args(remaining))
