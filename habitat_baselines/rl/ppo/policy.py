#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import abc

import torch
from gym import spaces
from torch import nn as nn

from habitat.config import Config
from habitat.tasks.nav.nav import (
    ImageGoalSensor,
    IntegratedPointGoalGPSAndCompassSensor,
    PointGoalSensor,
)
from habitat_baselines.common.baseline_registry import baseline_registry
from habitat_baselines.rl.models.rnn_state_encoder import (
    build_rnn_state_encoder,
)
from habitat_baselines.rl.models.simple_cnn import SimpleCNN
from habitat_baselines.utils.common import CategoricalNet


from src.main.curl_RL import CURL
from src.config_RL import getConfig, setSeed
from IPython import embed

class Policy(nn.Module, metaclass=abc.ABCMeta):
    def __init__(self, net, dim_actions):
        super().__init__()
        self.net = net
        self.dim_actions = dim_actions

        self.action_distribution = CategoricalNet(
            self.net.output_size, self.dim_actions
        )
        self.critic = CriticHead(self.net.output_size)

    def forward(self, *x):
        raise NotImplementedError

    def act(
        self,
        observations,
        rnn_hidden_states,
        prev_actions,
        masks,
        episode_id,
        deterministic=False,
    ):
        features, rnn_hidden_states = self.net(
            observations, rnn_hidden_states, prev_actions, masks, episode_id
        )
        distribution = self.action_distribution(features)
        value = self.critic(features)

        if deterministic:
            action = distribution.mode()
        else:
            action = distribution.sample()

        action_log_probs = distribution.log_probs(action)

        return value, action, action_log_probs, rnn_hidden_states

    def get_value(self, observations, rnn_hidden_states, prev_actions, masks, episode_id):
        features, _ = self.net(
            observations, rnn_hidden_states, prev_actions, masks, episode_id
        )
        return self.critic(features)

    def evaluate_actions(
        self, observations, rnn_hidden_states, prev_actions, masks, action, episode_id
    ):
        features, rnn_hidden_states = self.net(
            observations, rnn_hidden_states, prev_actions, masks, episode_id
        )
        distribution = self.action_distribution(features)
        value = self.critic(features)

        action_log_probs = distribution.log_probs(action)
        distribution_entropy = distribution.entropy()

        return value, action_log_probs, distribution_entropy, rnn_hidden_states

    @classmethod
    @abc.abstractmethod
    def from_config(cls, config, observation_space, action_space):
        pass


class CriticHead(nn.Module):
    def __init__(self, input_size):
        super().__init__()
        self.fc = nn.Linear(input_size, 1)
        nn.init.orthogonal_(self.fc.weight)
        nn.init.constant_(self.fc.bias, 0)

    def forward(self, x):
        return self.fc(x)


@baseline_registry.register_policy
class PointNavBaselinePolicy(Policy):
    def __init__(
        self,
        observation_space: spaces.Dict,
        action_space,
        hidden_size: int = 512,
        **kwargs
    ):
        super().__init__(
            PointNavBaselineNet(  # type: ignore
                observation_space=observation_space,
                hidden_size=hidden_size,
                **kwargs,
            ),
            action_space.n,
        )

    @classmethod
    def from_config(
        cls, config: Config, observation_space: spaces.Dict, action_space
    ):
        return cls(
            observation_space=observation_space,
            action_space=action_space,
            hidden_size=config.RL.PPO.hidden_size,
        )


class Net(nn.Module, metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def forward(self, observations, rnn_hidden_states, prev_actions, masks):
        pass

    @property
    @abc.abstractmethod
    def output_size(self):
        pass

    @property
    @abc.abstractmethod
    def num_recurrent_layers(self):
        pass

    @property
    @abc.abstractmethod
    def is_blind(self):
        pass


class PointNavBaselineNet(Net):
    r"""Network which passes the input image through CNN and concatenates
    goal vector with CNN's output and passes that through RNN.
    """

    def __init__(
        self,
        observation_space: spaces.Dict,
        hidden_size: int,
    ):
        super().__init__()

        if (
            IntegratedPointGoalGPSAndCompassSensor.cls_uuid
            in observation_space.spaces
        ):
            self._n_input_goal = observation_space.spaces[
                IntegratedPointGoalGPSAndCompassSensor.cls_uuid
            ].shape[0]
        elif PointGoalSensor.cls_uuid in observation_space.spaces:
            self._n_input_goal = observation_space.spaces[
                PointGoalSensor.cls_uuid
            ].shape[0]
        elif ImageGoalSensor.cls_uuid in observation_space.spaces:
            goal_observation_space = spaces.Dict(
                {"rgb": observation_space.spaces[ImageGoalSensor.cls_uuid]}
            )
            #self.goal_visual_encoder = SimpleCNN(
            #    goal_observation_space, hidden_size
            #)

            #conf = getConfig("curl_RL")
            #['curl']['path_goal_states'] = conf['test']['path_goal_states']
            #conf['curl']['load_goal_states'] = True
            #conf['curl']['device'] = torch.device("cuda" if torch.cuda.is_available() else "cpu")

            #curl = CURL(conf).cuda()
            #checkpoint = torch.load(conf['test']['path_weights'])
            #curl.load_state_dict(checkpoint['state_dict'])

            #for param in curl.parameters():
            #    param.requires_grad = False

            #self.goal_visual_encoder = curl



            self._n_input_goal = hidden_size

        self._hidden_size = hidden_size

        #self.visual_encoder = SimpleCNN(observation_space, hidden_size)

        conf = getConfig("curl_RL")
        #conf['curl']['path_goal_states'] = conf['test']['path_goal_states']
        #conf['curl']['load_goal_states'] = True
        #conf['curl']['device'] = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        curl = CURL(conf).cuda()
        checkpoint = torch.load(conf['test']['path_weights'])
        curl.load_state_dict(checkpoint['state_dict'])

        for param in curl.parameters():
            param.requires_grad = False

        self.visual_encoder = curl


        self.state_encoder = build_rnn_state_encoder(
            self._hidden_size + self._n_input_goal,
            self._hidden_size,
        )

        self.train()

    @property
    def output_size(self):
        return self._hidden_size

    @property
    def is_blind(self):
        return self.visual_encoder.is_blind

    @property
    def num_recurrent_layers(self):
        return self.state_encoder.num_recurrent_layers

    def forward(self, observations, rnn_hidden_states, prev_actions, masks, episode_id):
        if IntegratedPointGoalGPSAndCompassSensor.cls_uuid in observations:
            target_encoding = observations[
                IntegratedPointGoalGPSAndCompassSensor.cls_uuid
            ]

        elif PointGoalSensor.cls_uuid in observations:
            target_encoding = observations[PointGoalSensor.cls_uuid]
        elif ImageGoalSensor.cls_uuid in observations:
            image_goal = observations[ImageGoalSensor.cls_uuid]
            size = observations["rgb"].size(0)
            if size == 1:
                target_encoding = torch.FloatTensor(self.visual_encoder.forward_single(image_goal)).unsqueeze(0).cuda()
            else:
                target_encoding = torch.FloatTensor(self.visual_encoder.forward_single(image_goal)).cuda()


        # ONLY EPISOIDE ID FOR REWARD
        x = [target_encoding]
        
        size = observations["rgb"].size(0)
        if size == 1:
            #x = [self.visual_encoder.goal_states[int(episode_id[0])].unsqueeze(0)]
            perception_embed = torch.FloatTensor(self.visual_encoder.forward_single(observations["rgb"])).unsqueeze(0).cuda()
        else:
            #x = [self.visual_encoder.goal_states[int(episode_id[0])].unsqueeze(0).repeat(observations["rgb"].size(0),1)]
            perception_embed = torch.FloatTensor(self.visual_encoder.forward_single(observations["rgb"])).cuda()

        x = [perception_embed] + x

        
        x_out = torch.cat(x, dim=1)
        x_out, rnn_hidden_states = self.state_encoder(
            x_out, rnn_hidden_states, masks
        )

        return x_out, rnn_hidden_states
