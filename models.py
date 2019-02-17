import copy
from mpi4py import MPI
import numpy as np
import torch
from torch import nn
from torch.distributions import Normal


class Actor(nn.Module):
  def __init__(self, hidden_size, stochastic=True, layer_norm=False):
    super().__init__()
    layers = [nn.Linear(3, hidden_size), nn.Tanh(), nn.Linear(hidden_size, hidden_size), nn.Tanh(), nn.Linear(hidden_size, 1)]
    if layer_norm:
      layers = layers[:1] + [nn.LayerNorm(hidden_size)] + layers[1:3] + [nn.LayerNorm(hidden_size)] + layers[3:]  # Insert layer normalisation between fully-connected layers and nonlinearities
    self.policy = nn.Sequential(*layers)
    if stochastic:
      self.policy_log_std = nn.Parameter(torch.tensor([[0.]]))

  def forward(self, state):
    policy = self.policy(state)
    return policy


class TanhNormal(Normal):
  def rsample(self):
    return torch.tanh(self.loc + self.scale * torch.randn_like(self.scale))

  def sample(self):
    return self.rsample().detach()

  def log_prob(self, value):
    return super().log_prob(torch.atan(value)) - torch.log(1 - value.pow(2) + 1e-8) 

  @property
  def mean(self):
    return torch.tanh(super().mean)


class SoftActor(nn.Module):
  def __init__(self, hidden_size):
    super().__init__()
    layers = [nn.Linear(3, hidden_size), nn.Tanh(), nn.Linear(hidden_size, hidden_size), nn.Tanh(), nn.Linear(hidden_size, 2)]
    self.policy = nn.Sequential(*layers)

  def forward(self, state):
    policy_mean, policy_log_std = self.policy(state).chunk(2, dim=1)
    policy = TanhNormal(policy_mean, policy_log_std.exp())
    return policy


class Critic(nn.Module):
  def __init__(self, hidden_size, state_action=False, layer_norm=False):
    super().__init__()
    self.state_action = state_action
    layers = [nn.Linear(3 + (1 if state_action else 0), hidden_size), nn.Tanh(), nn.Linear(hidden_size, hidden_size), nn.Tanh(), nn.Linear(hidden_size, 1)]
    if layer_norm:
      layers = layers[:1] + [nn.LayerNorm(hidden_size)] + layers[1:3] + [nn.LayerNorm(hidden_size)] + layers[3:]  # Insert layer normalisation between fully-connected layers and nonlinearities
    self.value = nn.Sequential(*layers)

  def forward(self, state, action=None):
    if self.state_action:
      value = self.value(torch.cat([state, action], dim=1))
    else:
      value = self.value(state)
    return value.squeeze(dim=1)


class ActorCritic(nn.Module):
  def __init__(self, hidden_size):
    super().__init__()
    self.actor = Actor(hidden_size, stochastic=True)
    self.critic = Critic(hidden_size)

  def forward(self, state):
    policy = Normal(self.actor(state), self.actor.policy_log_std.exp())
    value = self.critic(state)
    return policy, value


class DQN(nn.Module):
  def __init__(self, hidden_size, num_actions=5):
    super().__init__()
    layers = [nn.Linear(3, hidden_size), nn.Tanh(), nn.Linear(hidden_size, hidden_size), nn.Tanh(), nn.Linear(hidden_size, num_actions)]
    self.dqn = nn.Sequential(*layers)

  def forward(self, state):
    values = self.dqn(state)
    return values


def create_target_network(network):
  target_network = copy.deepcopy(network)
  for param in target_network.parameters():
    param.requires_grad = False
  return target_network


def update_target_network(network, target_network, polyak_factor):
  for param, target_param in zip(network.parameters(), target_network.parameters()):
    target_param.data = polyak_factor * target_param.data + (1 - polyak_factor) * param.data


# Extracts a numpy vector of parameters/gradients from a network
def params_to_vec(network, mode):
  attr = 'data' if mode == 'params' else 'grad'
  return np.concatenate([getattr(param, attr).detach().view(-1).numpy() for param in network.parameters()])


# Copies a numpy vector of parameters/gradients into a network
def vec_to_params(vec, network, mode):
  attr = 'data' if mode == 'params' else 'grad'
  param_pointer = 0
  for param in network.parameters():
    getattr(param, attr).copy_(torch.from_numpy(vec[param_pointer:param_pointer + param.data.numel()]).view_as(param.data))
    param_pointer += param.data.numel()


# Synchronises a network's gradients across processes
def sync_grads(comm, network):
  grad_vec_send = params_to_vec(network, mode='grads')
  grad_vec_recv = np.zeros_like(grad_vec_send)
  comm.Allreduce(grad_vec_send, grad_vec_recv, op=MPI.SUM)
  vec_to_params(grad_vec_recv / comm.Get_size(), network, mode='grads')
