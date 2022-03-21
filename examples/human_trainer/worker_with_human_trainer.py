from typing import Any
import numpy

from redis import Redis

from rlgym.envs import Match
from rlgym.utils.gamestates import PlayerData, GameState
from rlgym.utils.terminal_conditions.common_conditions import GoalScoredCondition, TimeoutCondition
from rlgym.utils.reward_functions.default_reward import DefaultReward
from rlgym.utils.state_setters.default_state import DefaultState
from rlgym.utils.obs_builders.advanced_obs import AdvancedObs
from rlgym.utils.action_parsers.discrete_act import DiscreteAction

from rocket_learn.rollout_generator.redis_rollout_generator import RedisRolloutWorker
from rocket_learn.agent.pretrained_agents.human_agent import HumanAgent


# rocket-learn always expects a batch dimension in the built observation
class ExpandAdvancedObs(AdvancedObs):
    def build_obs(self, player: PlayerData, state: GameState, previous_action: numpy.ndarray) -> Any:
        obs = super(ExpandAdvancedObs, self).build_obs(player, state, previous_action)
        return numpy.expand_dims(obs, 0)

"""

Allows the worker to run a human player, letting the AI play against and learn from human interation.

Important things to note:

-The human will always be blue due to RLGym camera constraints
-Attempting to run a human trainer and pretrained agents will cause the pretrained agents to be ignored. They will
never show up.

"""

if __name__ == "__main__":
    match = Match(
        game_speed=1,
        self_play=True,
        team_size=1,
        state_setter=DefaultState(),
        obs_builder=ExpandAdvancedObs(),
        action_parser=DiscreteAction(),
        terminal_conditions=[TimeoutCondition(round(2000)),
                             GoalScoredCondition()],
        reward_function=DefaultReward()
    )

    human = HumanAgent()

    r = Redis(host="127.0.0.1", password="you_better_use_a_password")
    RedisRolloutWorker(r, "exampleHumanWorker", match, human_agent=human, past_version_prob=.05).run()
