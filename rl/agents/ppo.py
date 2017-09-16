import os
import itertools

import numpy as np
from keras import optimizers, Input
from keras.engine import Model
from keras.layers import Lambda

from rl.core import Agent
from rl.util import clone_optimizer, clone_model, GeneralizedAdvantageEstimator

import keras.backend as K


class PPOAgent(Agent):
    # Note on network architecture:
    # actor should take as input the current state and candidate action, and should output a scalar
    # representing the probability of taking that action.
    # critic's only input is the current state, and should output a scalar representing estimated
    # value of this state
    def __init__(self, actor, critic, memory, epsilon=0.2, nb_actor=3, nb_steps=1000, epoch=5, **kwargs):
        super(Agent, self).__init__(**kwargs)

        # Parameters.
        self.epsilon = epsilon
        self.nb_actor = nb_actor
        self.nb_steps = nb_steps
        self.epoch = epoch

        # Related objects.
        self.actor = actor
        self.critic = critic
        self.memory = memory

    def compile(self, optimizer, metrics=[]):
        # TODO
        if type(optimizer) in (list, tuple):
            if len(optimizer) != 2:
                raise ValueError('More than two optimizers provided. Please only provide a maximum of two optimizers, the first one for the actor and the second one for the critic.')
            actor_optimizer, critic_optimizer = optimizer
        else:
            actor_optimizer = optimizer
            critic_optimizer = clone_optimizer(optimizer)
        if type(actor_optimizer) is str:
            actor_optimizer = optimizers.get(actor_optimizer)
        if type(critic_optimizer) is str:
            critic_optimizer = optimizers.get(critic_optimizer)
        assert actor_optimizer != critic_optimizer

        # Compile networks:
        # Critic is used in a standard way, so nothing special.
        # Actor is the ephemeral network that is directly trained,
        # While target_network is the actual actor network and is updated with the weight of actor
        # after each round of training
        self.target_actor = clone_model(self.actor, self.custom_model_objects)
        self.target_actor.set_weights(self.actor.get_weights())
        self.target_actor.name += '_copy'
        for layer in self.target_actor.layers:
            layer.trainable = False
        #self.actor.compile(optimizer='sgd', loss='mse')
        self.critic.compile(optimizer=critic_optimizer)

        # TODO: Model for the overall objective
        action = Input(name='action')
        state = Input(name='state')
        advantage = Input(name='advantage')
        prob_theta = self.actor(action, state)
        prob_thetaold = self.target_actor(action, state)
        def clipped_loss(args):
            prob_theta, prob_thetaold, advantage = args
            prob_ratio = prob_theta / prob_thetaold
            return K.minimum(prob_ratio * advantage, K.clip(prob_ratio, 1-self.epsilon, 1+self.epsilon) * advantage)
        loss_out = Lambda(clipped_loss, name='loss')([prob_theta, prob_thetaold, advantage])
        trainable_model = Model(inputs=[action, state, advantage], outputs=loss_out)
        losses = [ lambda sample_out, network_out: network_out ]
        trainable_model.compile(optimizer=optimizer, loss=losses)
        self.trainable_model = trainable_model

        # Other init
        self.round = 0

    def load_weights(self, filepath):
        filename, extension = os.path.splitext(filepath)
        actor_filepath = filename + '_actor' + extension
        critic_filepath = filename + '_critic' + extension
        self.actor.load_weights(actor_filepath)
        self.critic.load_weights(critic_filepath)

    def save_weights(self, filepath, overwrite=False):
        filename, extension = os.path.splitext(filepath)
        actor_filepath = filename + '_actor' + extension
        critic_filepath = filename + '_critic' + extension
        self.actor.save_weights(actor_filepath, overwrite=overwrite)
        self.critic.save_weights(critic_filepath, overwrite=overwrite)

    def forward(self, observation):
        # TODO
        prob_dist = self.target_actor.predict_on_batch({ 'state': np.repeat(observation, self.nb_action),
                                                         'action': np.arange(self.nb_action) })
        return self.policy.select_action(prob_dist)

    @property
    def layers(self):
        return self.actor.layers[:] + self.critic.layers[:]

    def _get_sample_batch(self):
        experiences, info = self.memory.sample(self.batch_size)
        assert len(experiences) == self.batch_size
        assert len(info) == self.batch_size

        # Start by extracting the necessary parameters (we use a vectorized implementation).
        state0_batch = []
        reward_batch = []
        action_batch = []
        terminal1_batch = []
        state1_batch = []
        gae_batch = []
        for e, gae in zip(experiences, info):  # TODO: Okay to use zip?
            state0_batch.append(e.state0)
            state1_batch.append(e.state1)
            reward_batch.append(e.reward)
            action_batch.append(e.action)
            terminal1_batch.append(0. if e.terminal1 else 1.)
            gae_batch.append(gae)

        # Prepare and validate parameters.
        state0_batch = self.process_state_batch(state0_batch)
        state1_batch = self.process_state_batch(state1_batch)
        terminal1_batch = np.array(terminal1_batch)
        reward_batch = np.array(reward_batch)
        action_batch = np.array(action_batch)
        assert reward_batch.shape == (self.batch_size,)
        assert terminal1_batch.shape == reward_batch.shape
        assert action_batch.shape == (self.batch_size, self.nb_actions)

        return state0_batch, reward_batch, action_batch, terminal1_batch, state1_batch, gae_batch

    def backward(self, reward, terminal=False):
        # TODO: Just a sketch
        # Store most recent experience in memory.
        #if self.step % self.memory_interval == 0:
        if self.step == 0:
            self.done_this_round = False
        if not self.done_this_round:
            self.memory.append(self.recent_observation, self.recent_action, reward, terminal,
                        training=self.training)
            if terminal or self.step == self.nb_steps:
                # Compute and store GAE
                state_history, _, reward_history, _ = self.memory.take_recent(self.step)
                gae = GeneralizedAdvantageEstimator(self.critic, state_history, reward_history, self.gamma, self.lamb)
                self.memory.add_info(self.step, gae)
                self.round += 1
                self.done_this_round = True

        # Train network every nb_actor rounds of simulation
        if self.round % self.nb_actor == 0:
            for _ in itertools.repeat(None, self.epoch):
                state0_batch, reward_batch, action_batch, terminal1_batch, state1_batch, gae_batch\
                    = self._get_sample_batch()

                # Train actor with one batch
                dummy_targets = np.zeros((self.batch_size,))
                self.trainable_model.train_on_batch([action_batch, state0_batch, gae_batch], [dummy_targets])

            # Update actor
            self.target_actor.set_weights(self.actor.get_weights())

            for _ in itertools.repeat(None, self.epoch):
                state0_batch, reward_batch, action_batch, terminal1_batch, state1_batch, gae_batch \
                    = self._get_sample_batch()

                # Update critic
                target_actions = self.target_actor.predict_on_batch(state1_batch)
                assert target_actions.shape == (self.batch_size, self.nb_actions)
                if len(self.critic.inputs) >= 3:
                    state1_batch_with_action = state1_batch[:]
                else:
                    state1_batch_with_action = [state1_batch]
                state1_batch_with_action.insert(self.critic_action_input_idx, target_actions)
                target_q_values = self.target_critic.predict_on_batch(state1_batch_with_action).flatten()
                assert target_q_values.shape == (self.batch_size,)

                # Compute r_t + gamma * max_a Q(s_t+1, a) and update the target ys accordingly,
                # but only for the affected output units (as given by action_batch).
                discounted_reward_batch = self.gamma * target_q_values
                discounted_reward_batch *= terminal1_batch
                assert discounted_reward_batch.shape == reward_batch.shape
                targets = (reward_batch + discounted_reward_batch).reshape(self.batch_size, 1)

                # Perform a single batch update on the critic network.
                if len(self.critic.inputs) >= 3:
                    state0_batch_with_action = state0_batch[:]
                else:
                    state0_batch_with_action = [state0_batch]
                state0_batch_with_action.insert(self.critic_action_input_idx, action_batch)
                metrics = self.critic.train_on_batch(state0_batch_with_action, targets)
                if self.processor is not None:
                    metrics += self.processor.metrics

