from functools import partial
import jax.numpy as jnp
import jax
from jax import random, jit, vmap
import haiku as hk
from typing import Optional, Mapping, Type, Callable, Iterable, Any, Sequence, Union, Tuple
import nux.util as util
from nux.internal.layer import Layer
import nux

__all__ = ["sequential",
           "factored",
           "multi_scale",
           "reverse_flow",
           "track"]

################################################################################################################

class sequential(Layer):

  def __init__(self,
               *layers: Iterable[Callable],
               name: str="sequential"
  ):
    """ Create a flow sequentially
    Args:
      layers: An iterable that contains flow layers
      name  : Optional name for this module.
    """
    super().__init__(name=name)
    self.layers = tuple(layers)

  def call(self,
           inputs: Mapping[str, jnp.ndarray],
           rng: jnp.ndarray=None,
           sample: Optional[bool]=False,
           accumulate: Iterable[str]=["log_det"],
           **kwargs
  ) -> Mapping[str, jnp.ndarray]:

    n_layers = len(self.layers)
    iter_layers = self.layers if sample == False else self.layers[::-1]

    # Want to make sure that we're passing all inputs/outputs to the next layer
    final_outputs = inputs.copy()
    accumulated_outputs = dict([(name, jnp.array(0.0)) for name in accumulate])
    accumulated_found = dict([(name, False) for name in accumulate])

    # Split the random key
    rngs = random.split(rng, n_layers) if rng is not None else [None]*n_layers

    # Run the rest of the layers
    layer_inputs = inputs.copy()
    for i, (layer, rng) in enumerate(zip(iter_layers, rngs)):
      outputs = layer(layer_inputs, rng, sample=sample, **kwargs)
      layer_inputs["x"] = outputs["x"]
      final_outputs.update(outputs)

      # Remember to accumulate the outputs
      for name in accumulated_outputs.keys():
        if name in outputs:
          accumulated_outputs[name] += outputs[name]
          accumulated_found[name] = True

    # Swap in the accumulated outputs
    for name, val in accumulated_outputs.items():
      if accumulated_found[name]:
        final_outputs[name] = val

    return final_outputs

################################################################################################################

class factored(Layer):

  def __init__(self,
               *layers: Iterable[Callable],
               axis: Optional[int]=-1,
               first_factor_ratio: int=2,
               name: str="sequential"
  ):
    """ Create a flow in parallel.  This is basically like using the chain
        rule and applying a flow to each part.
    Args:
      layers            : An iterable that contains flow layers
      axis              : Which axis to factor on
      first_factor_ratio: How large the first component should be compared to the second
      name              : Optional name for this module.
    """
    super().__init__(name=name)
    self.layers             = tuple(layers)
    self.axis               = axis
    self.first_factor_ratio = first_factor_ratio

  def call(self,
           inputs: Mapping[str, jnp.ndarray],
           rng: jnp.ndarray=None,
           sample: Optional[bool]=False,
           accumulate: Iterable[str]=["log_det"],
           **kwargs
  ) -> Mapping[str, jnp.ndarray]:

    n_layers = len(self.layers)
    iter_layers = self.layers if sample == False else self.layers[::-1]

    # Want to make sure that we're passing all inputs/outputs to the next layer
    final_outputs = inputs.copy()
    accumulated_outputs = dict([(name, jnp.array(0.0)) for name in accumulate])
    accumulated_found = dict([(name, False) for name in accumulate])

    # Split x
    # split_size = inputs["x"].shape[self.axis]/n_layers
    # split_size = jnp.ceil(split_size).astype(int)
    # split_idx = jnp.array([i*split_size for i in range(1, n_layers)])
    # xs = jnp.split(inputs["x"], indices_or_sections=split_idx, axis=self.axis)

    if n_layers == 2:
      split_idx = inputs["x"].shape[self.axis]//self.first_factor_ratio
      xs = jnp.split(inputs["x"], indices_or_sections=(split_idx,), axis=self.axis)
      # xs = jnp.split(inputs["x"], n_layers, self.axis)
    else:
      assert 0, "Not implemented"

    zs = []

    # Split the random key
    rngs = random.split(rng, n_layers) if rng is not None else [None]*n_layers

    # Run each of the flows on a part of x
    layer_inputs = inputs.copy()
    for i, (x, layer, rng) in enumerate(zip(xs, self.layers, rngs)):
      layer_inputs["x"] = x
      outputs = layer(layer_inputs, rng, sample=sample, **kwargs)
      final_outputs.update(outputs)
      zs.append(outputs["x"])

      # Remember to accumulate the outputs
      for name in accumulated_outputs.keys():
        if name in outputs:
          accumulated_outputs[name] += outputs[name]
          accumulated_found[name] = True

    # Swap in the accumulated outputs
    for name, val in accumulated_outputs.items():
      if accumulated_found[name]:
        final_outputs[name] = val

    # Recombine the data
    z = jnp.concatenate(zs, self.axis)
    final_outputs["x"] = z
    return final_outputs

################################################################################################################

class multi_scale(Layer):

  def __init__(self,
               flow,
               name: str="multi_scale",
  ):
    """ Use a flow in a multiscale architecture as described in RealNVP https://arxiv.org/pdf/1605.08803.pdf
        Factors half of the dimensions.
    Args:
      flow: The flow to use
      name: Optional name for this module.
    """
    super().__init__(name=name)
    self.flow = flow

  def call(self,
           inputs: Mapping[str, jnp.ndarray],
           rng: jnp.ndarray=None,
           sample: Optional[bool]=False,
           **kwargs
  ) -> Mapping[str, jnp.ndarray]:
    x = inputs["x"]

    # Split the input vector on the channel dimension
    xa, xb = jnp.split(x, indices_or_sections=jnp.array([x.shape[-1]//2]), axis=-1)

    # Run a flow on only one half
    factored_inputs = inputs.copy()
    factored_inputs["x"] = xb
    outputs = self.flow(factored_inputs, rng, sample=sample, **kwargs)

    z = jnp.concatenate([xa, outputs["x"]], axis=-1)

    outputs["x"] = z
    return outputs

################################################################################################################

class reverse_flow(Layer):

  def __init__(self,
               flow,
               name: str="reverse_flow",
  ):
    """ Reverse the direction of a flow.  Useful if one direction is faster than the other.
    Args:
      flow: The flow to use
      name: Optional name for this module.
    """
    super().__init__(name=name)
    self.flow = flow

  def call(self,
           inputs: Mapping[str, jnp.ndarray],
           rng: jnp.ndarray=None,
           sample: Optional[bool]=False,
           **kwargs
  ) -> Mapping[str, jnp.ndarray]:

    outputs = self.flow(inputs, rng, not sample, **kwargs)

    outputs["log_det"] *= -1

    return outputs

################################################################################################################

class track(Layer):

  def __init__(self, flow, name: str, reducer: Callable=None, **kwargs):
    self.name = name
    self.flow = flow
    self.reducer = reducer
    super().__init__(name=name, **kwargs)

  def call(self,
           inputs: Mapping[str, jnp.ndarray],
           rng: jnp.ndarray=None,
           sample: Optional[bool]=False,
           **kwargs
  ) -> Mapping[str, jnp.ndarray]:

    flow_outputs = self.flow(inputs, rng, sample=sample, **kwargs)
    outputs = flow_outputs.copy()
    if self.reducer is None:
      outputs[self.name] = flow_outputs
    else:
      outputs[self.name] = self.reducer(flow_outputs)
    return outputs