import os
import inspect
import glob
import yaml
import importlib

import gym
try:
    import pybullet_envs
except ImportError:
    pybullet_envs = None

from gym.envs.registration import load

from torchy_baselines.common.monitor import Monitor
# from torchy_baselines.common import logger
from torchy_baselines import A2C, PPO, SAC, TD3, CEMRL
from torchy_baselines.common.vec_env import DummyVecEnv, VecNormalize, \
    VecFrameStack, SubprocVecEnv
# from torchy_baselines.common.cmd_util import make_atari_env
from torchy_baselines.common.utils import set_random_seed

ALGOS = {
    'a2c': A2C,
    'cemrl': CEMRL,
    'ppo': PPO,
    'sac': SAC,
    'td3': TD3
}


def flatten_dict_observations(env):
    assert isinstance(env.observation_space, gym.spaces.Dict)
    keys = env.observation_space.spaces.keys()
    return gym.wrappers.FlattenDictWrapper(env, dict_keys=list(keys))


def get_wrapper_class(hyperparams):
    """
    Get one or more Gym environment wrapper class specified as a hyper parameter
    "env_wrapper".
    e.g.
    env_wrapper: gym_minigrid.wrappers.FlatObsWrapper

    for multiple, specify a list:

    env_wrapper:
        - utils.wrappers.PlotActionWrapper
        - utils.wrappers.TimeFeatureWrapper


    :param hyperparams: (dict)
    :return: a subclass of gym.Wrapper (class object) you can use to
             create another Gym env giving an original env.
    """

    def get_module_name(wrapper_name):
        return '.'.join(wrapper_name.split('.')[:-1])

    def get_class_name(wrapper_name):
        return wrapper_name.split('.')[-1]

    if 'env_wrapper' in hyperparams.keys():
        wrapper_name = hyperparams.get('env_wrapper')
        if not isinstance(wrapper_name, list):
            wrapper_names = [wrapper_name]
        else:
            wrapper_names = wrapper_name

        wrapper_classes = []
        wrapper_kwargs = []
        # Handle multiple wrappers
        for wrapper_name in wrapper_names:
            # Handle keyword arguments
            if isinstance(wrapper_name, dict):
                assert len(wrapper_name) == 1
                wrapper_dict = wrapper_name
                wrapper_name = list(wrapper_dict.keys())[0]
                kwargs = wrapper_dict[wrapper_name]
            else:
                kwargs = {}
            wrapper_module = importlib.import_module(get_module_name(wrapper_name))
            wrapper_class = getattr(wrapper_module, get_class_name(wrapper_name))
            wrapper_classes.append(wrapper_class)
            wrapper_kwargs.append(kwargs)

        def wrap_env(env):
            """
            :param env: (gym.Env)
            :return: (gym.Env)
            """
            for wrapper_class, kwargs in zip(wrapper_classes, wrapper_kwargs):
                env = wrapper_class(env, **kwargs)
            return env
        return wrap_env
    else:
        return None


def make_env(env_id, rank=0, seed=0, log_dir=None, wrapper_class=None):
    """
    Helper function to multiprocess training
    and log the progress.

    :param env_id: (str)
    :param rank: (int)
    :param seed: (int)
    :param log_dir: (str)
    :param wrapper_class: (Type[gym.Wrapper]) a subclass of gym.Wrapper to wrap the original
                    env with
    """
    if log_dir is not None:
        os.makedirs(log_dir, exist_ok=True)

    def _init():
        set_random_seed(seed + rank)
        env = gym.make(env_id)

        # Dict observation space is currently not supported.
        # https://github.com/hill-a/stable-baselines/issues/321
        # We allow a Gym env wrapper (a subclass of gym.Wrapper)
        if wrapper_class:
            env = wrapper_class(env)

        env.seed(seed + rank)
        log_file = os.path.join(log_dir, str(rank)) if log_dir is not None else None
        env = Monitor(env, log_file)
        return env

    return _init


def create_test_env(env_id, n_envs=1, is_atari=False,
                    stats_path=None, seed=0,
                    log_dir='', should_render=True, hyperparams=None):
    """
    Create environment for testing a trained agent

    :param env_id: (str)
    :param n_envs: (int) number of processes
    :param is_atari: (bool)
    :param stats_path: (str) path to folder containing saved running averaged
    :param seed: (int) Seed for random number generator
    :param log_dir: (str) Where to log rewards
    :param should_render: (bool) For Pybullet env, display the GUI
    :param hyperparams: (dict) Additional hyperparams (ex: n_stack)
    :return: (gym.Env)
    """
    # HACK to save logs
    # if log_dir is not None:
    #     os.environ["OPENAI_LOG_FORMAT"] = 'csv'
    #     os.environ["OPENAI_LOGDIR"] = os.path.abspath(log_dir)
    #     os.makedirs(log_dir, exist_ok=True)
    #     logger.configure()

    # Create the environment and wrap it if necessary
    env_wrapper = get_wrapper_class(hyperparams)
    if 'env_wrapper' in hyperparams.keys():
        del hyperparams['env_wrapper']

    if is_atari:
        raise NotImplementedError()
        # print("Using Atari wrapper")
        # env = make_atari_env(env_id, num_env=n_envs, seed=seed)
        # Frame-stacking with 4 frames
        # env = VecFrameStack(env, n_stack=4)
    elif n_envs > 1:
        # start_method = 'spawn' for thread safe
        env = SubprocVecEnv([make_env(env_id, i, seed, log_dir, wrapper_class=env_wrapper) for i in range(n_envs)])
    # Pybullet envs does not follow gym.render() interface
    elif "Bullet" in env_id:
        spec = gym.envs.registry.env_specs[env_id]  # pytype: disable=module-attr
        try:
            class_ = load(spec.entry_point)
        except AttributeError:
            # Backward compatibility with gym
            class_ = load(spec._entry_point)
        # HACK: force SubprocVecEnv for Bullet env that does not
        # have a render argument
        render_name = None
        use_subproc = 'renders' not in inspect.getfullargspec(class_.__init__).args
        if not use_subproc:
            render_name = 'renders'
        # Dev branch of pybullet
        # use_subproc = use_subproc and 'render' not in inspect.getfullargspec(class_.__init__).args
        # if not use_subproc and render_name is None:
        #     render_name = 'render'

        # Create the env, with the original kwargs, and the new ones overriding them if needed
        def _init():
            # TODO: fix for pybullet locomotion envs
            env = class_(**{**spec._kwargs}, **{render_name: should_render})
            env.seed(0)
            if log_dir is not None:
                env = Monitor(env, os.path.join(log_dir, "0"))
            return env

        if use_subproc:
            env = SubprocVecEnv([make_env(env_id, 0, seed, log_dir, wrapper_class=env_wrapper)])
        else:
            env = DummyVecEnv([_init])
    else:
        env = DummyVecEnv([make_env(env_id, 0, seed, log_dir, wrapper_class=env_wrapper)])

    # Load saved stats for normalizing input and rewards
    # And optionally stack frames
    if stats_path is not None:
        if hyperparams['normalize']:
            print("Loading running average")
            print("with params: {}".format(hyperparams['normalize_kwargs']))
            env = VecNormalize(env, training=False, **hyperparams['normalize_kwargs'])

            if os.path.exists(os.path.join(stats_path, 'vecnormalize.pkl')):
                env = VecNormalize.load(os.path.join(stats_path, 'vecnormalize.pkl'), env)
                # Deactivate training and reward normalization
                env.training = False
                env.norm_reward = False
            else:
                # Legacy:
                env.load_running_average(stats_path)

        n_stack = hyperparams.get('frame_stack', 0)
        if n_stack > 0:
            print("Stacking {} frames".format(n_stack))
            env = VecFrameStack(env, n_stack)
    return env


def linear_schedule(initial_value):
    """
    Linear learning rate schedule.

    :param initial_value: (float or str)
    :return: (function)
    """
    if isinstance(initial_value, str):
        initial_value = float(initial_value)

    def func(progress):
        """
        Progress will decrease from 1 (beginning) to 0
        :param progress: (float)
        :return: (float)
        """
        return progress * initial_value

    return func


def linear_schedule_std(initial_value, final_value=-5.5):
    """
    Linear learning rate schedule for log std.
    It will decrease linearly from initial_value to final_value
    (by default exp(-5.5) = 0.004 will be the final value of the std)

    :param initial_value: (float or str)
    :param final_value: (float)
    :return: (function)
    """
    if isinstance(initial_value, str):
        initial_value = float(initial_value)

    def func(progress):
        """
        Progress will decrease from 1 (beginning) to 0
        :param progress: (float)
        :return: (float)
        """
        return progress * initial_value + (1 - progress) * final_value

    return func


def get_trained_models(log_folder):
    """
    :param log_folder: (str) Root log folder
    :return: (dict) Dict representing the trained agent
    """
    algos = os.listdir(log_folder)
    trained_models = {}
    for algo in algos:
        for ext in ['zip', 'pkl']:
            for env_id in glob.glob('{}/{}/*.{}'.format(log_folder, algo, ext)):
                # Retrieve env name
                env_id = env_id.split('/')[-1].split('.{}'.format(ext))[0]
                trained_models['{}-{}'.format(algo, env_id)] = (algo, env_id)
    return trained_models


def get_latest_run_id(log_path, env_id):
    """
    Returns the latest run number for the given log name and log path,
    by finding the greatest number in the directories.

    :param log_path: (str) path to log folder
    :param env_id: (str)
    :return: (int) latest run number
    """
    max_run_id = 0
    for path in glob.glob(log_path + "/{}_[0-9]*".format(env_id)):
        file_name = path.split("/")[-1]
        ext = file_name.split("_")[-1]
        if env_id == "_".join(file_name.split("_")[:-1]) and ext.isdigit() and int(ext) > max_run_id:
            max_run_id = int(ext)
    return max_run_id


def get_saved_hyperparams(stats_path, norm_reward=False, test_mode=False):
    """
    :param stats_path: (str)
    :param norm_reward: (bool)
    :param test_mode: (bool)
    :return: (dict, str)
    """
    hyperparams = {}
    if not os.path.isdir(stats_path):
        stats_path = None
    else:
        config_file = os.path.join(stats_path, 'config.yml')
        if os.path.isfile(config_file):
            # Load saved hyperparameters
            with open(os.path.join(stats_path, 'config.yml'), 'r') as f:
                hyperparams = yaml.load(f, Loader=yaml.UnsafeLoader)  # pytype: disable=module-attr
            hyperparams['normalize'] = hyperparams.get('normalize', False)
        else:
            obs_rms_path = os.path.join(stats_path, 'obs_rms.pkl')
            hyperparams['normalize'] = os.path.isfile(obs_rms_path)

        # Load normalization params
        if hyperparams['normalize']:
            if isinstance(hyperparams['normalize'], str):
                normalize_kwargs = eval(hyperparams['normalize'])
                if test_mode:
                    normalize_kwargs['norm_reward'] = norm_reward
            else:
                normalize_kwargs = {'norm_obs': hyperparams['normalize'], 'norm_reward': norm_reward}
            hyperparams['normalize_kwargs'] = normalize_kwargs
    return hyperparams, stats_path
