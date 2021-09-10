import io
import os
import pickle
import time
from typing import Iterator
from uuid import uuid4

import numpy as np
import torch
import wandb
from redis import Redis
from trueskill import Rating, rate

from rlgym.envs import Match
from rlgym.gamelaunch import LaunchPreference
from rlgym.gym import Gym
from rocket_learn.utils.experiencebuffer import ExperienceBuffer
from rocket_learn.rollout_generators.base_rolloutgenerator import BaseRolloutGenerator
from rocket_learn.utils import util
from rocket_learn.utils.util import softmax

# Constants for consistent key lookup
QUALITIES = "qualities"
MODEL_LATEST = "model-latest"
ROLLOUTS = "rollout"
VERSION_LATEST = "model-version"
OPPONENT_MODELS = "opponent-models"
WORKER_IDS = "worker-ids"
_ALL = (QUALITIES, MODEL_LATEST, ROLLOUTS, VERSION_LATEST, OPPONENT_MODELS, WORKER_IDS)


# Helper methods for easier changing of byte conversion
def _serialize(obj):
    return pickle.dumps(obj)


def _unserialize(obj):
    return pickle.loads(obj)


def _serialize_model(mdl):
    buf = io.BytesIO()
    torch.save([mdl.actor, mdl.critic, mdl.shared], buf)
    return buf


def _unserialize_model(buf):
    return torch.load(buf)


class RedisRolloutGenerator(BaseRolloutGenerator):
    def __init__(self, redis: Redis, save_every=10, logger=None, clear=True):
        # **DEFAULT NEEDS TO INCORPORATE BASIC SECURITY, THIS IS NOT SUFFICIENT**
        self.redis = redis
        self.n_updates = 0
        self.save_every = save_every

        self.logger = logger

        # TODO saving/loading
        if clear:
            for key in _ALL:
                if self.redis.exists(key) > 0:
                    self.redis.delete(key)

    def generate_rollouts(self) -> Iterator[ExperienceBuffer]:
        while True:
            rollout_bytes = self.redis.blpop(ROLLOUTS)[1]

            rollout_data, uuid, name, result = _unserialize(rollout_bytes)
            latest_version = int(self.redis.get(VERSION_LATEST))
            # TODO ensure rollouts are not generated by very old model versions
            # TODO log uuid and name

            blue_players = sum(divmod(len(rollout_data), 2))
            blue, orange = [], []
            rollouts = []
            for n, (rollout, version) in enumerate(rollout_data):
                rating = _unserialize(self.redis.lindex(QUALITIES, version))
                if version == latest_version:
                    rollouts.append(rollout)
                if n < blue_players:
                    blue.append(rating)
                else:
                    orange.append(rating)

            if result >= 0:
                r1, r2 = rate((blue, orange), ranks=(0, result))
            else:
                r2, r1 = rate((orange, blue))

            versions = {}
            for rating, (rollout, version) in zip(r1 + r2, rollout_data):
                versions.setdefault(version, []).append(rating)

            del versions[0]  # v0 rating is fixed
            for version, ratings in versions.items():
                avg_rating = Rating((sum(r.mu for r in ratings) / len(ratings)),
                                    (sum(r.sigma for r in ratings) / len(ratings)))
                self.redis.lset(QUALITIES, version, _serialize(avg_rating))

            yield from rollouts

    def _add_opponent(self, agent):
        # Add to list
        self.redis.rpush(OPPONENT_MODELS, agent)
        # Set quality
        ratings = [_unserialize(v) for v in self.redis.lrange(QUALITIES, 0, -1)]
        if ratings:
            self.logger.log({
                "qualities": wandb.plot.line_series(np.arange(len(ratings)), [np.array(r.mu for r in ratings)],
                                                    ["quality"], "Qualities", "version")
            })
            quality = Rating(ratings[-1].mu)
        else:
            quality = Rating(0)
        self.redis.rpush(QUALITIES, _serialize(quality))

    def update_parameters(self, new_params):
        model_bytes = _serialize(new_params)
        self.redis.set(MODEL_LATEST, model_bytes)
        self.redis.set(VERSION_LATEST, self.n_updates)

        # TODO Idea: workers send name to identify who contributed rollouts,
        # keep track of top rollout contributors (each param update and total)
        # Also UID to keep track of current number of contributing workers?

        if self.n_updates % self.save_every == 0:
            # self.redis.set(MODEL_N.format(self.n_updates // self.save_every), model_bytes)
            self._add_opponent(model_bytes)

        self.n_updates += 1


class RedisRolloutWorker:  # Provides RedisRolloutGenerator with rollouts via a Redis server
    def __init__(self, redis: Redis, name: str, match: Match, current_version_prob=.9):
        # TODO model or config+params so workers can recreate just from redis connection?
        self.redis = redis
        self.name = name

        self.current_agent = _unserialize(self.redis.get(MODEL_LATEST))
        self.current_version_prob = current_version_prob

        # **DEFAULT NEEDS TO INCORPORATE BASIC SECURITY, THIS IS NOT SUFFICIENT**
        self.uuid = str(uuid4())
        self.redis.rpush(WORKER_IDS, self.uuid)
        print("Started worker", self.uuid, "on host", self.redis.connection_pool.connection_kwargs.get("host"),
              "under name", name)  # TODO log instead
        self.match = match
        self.env = Gym(match=self.match, pipe_id=os.getpid(), launch_preference=LaunchPreference.EPIC_LOGIN_TRICK,
                       use_injector=False)
        self.n_agents = self.match.agents

    def _get_opponent_index(self):
        # Get qualities
        qualities = np.asarray([_unserialize(v).mu for v in self.redis.lrange(QUALITIES, 0, -1)])
        # Pick opponent
        probs = softmax(qualities / np.log(10))
        index = np.random.choice(len(probs), p=probs)
        return index, probs[index]

    def run(self):  # Mimics Thread
        n = 0
        while True:
            model_bytes = self.redis.get(MODEL_LATEST)
            latest_version = self.redis.get(VERSION_LATEST)
            if model_bytes is None:
                time.sleep(1)
                continue  # Wait for model to get published
            updated_agent = _unserialize(model_bytes)
            latest_version = int(latest_version)

            n += 1

            self.current_agent = updated_agent

            # TODO customizable past agent selection, should team only be same agent?
            agents = [(self.current_agent, latest_version, self.current_version_prob)]  # Use at least one current agent

            if self.n_agents > 1:
                # Ensure final proportion is same
                adjusted_prob = (self.current_version_prob * self.n_agents - 1) / (self.n_agents - 1)
                for i in range(self.n_agents - 1):
                    is_current = np.random.random() < adjusted_prob
                    if not is_current:
                        index, prob = self._get_opponent_index()
                        version = index
                        selected_agent = _unserialize(self.redis.lindex(OPPONENT_MODELS, index))
                    else:
                        prob = self.current_version_prob
                        version = latest_version
                        selected_agent = self.current_agent

                    agents.append((selected_agent, version, prob))

            np.random.shuffle(agents)

            rollouts, result = util.generate_episode(self.env, [agent for agent, version, prob in agents])

            rollout_data = []
            for rollout, (agent, version, prob) in zip(rollouts, agents):
                rollout_data.append((rollout, version))

            self.redis.rpush(ROLLOUTS, _serialize((rollout_data, self.uuid, self.name, result)))
