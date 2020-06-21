import jax
import jax.nn.initializers as jaxinit
import jax.numpy as jnp
from jax import random, vmap, jit
from functools import partial
import src.util as util
import src.flows.base as base

@base.auto_batch
def ActNorm(log_s_init=jaxinit.zeros, b_init=jaxinit.zeros, name='act_norm'):
    # language=rst
    """
    Act normalization
    """
    multiply_by = None

    def apply_fun(params, state, inputs, reverse=False, **kwargs):
        x = inputs['x']

        if(reverse == False):
            z = (x - params['b'])*jnp.exp(-params['log_s'])
        else:
            z = jnp.exp(params['log_s'])*x + params['b']

        log_det = -params['log_s'].sum()

        # Need to multiply by the height/width if we have an image
        log_det *= multiply_by

        outputs = {'x': z, 'log_det': log_det}
        return outputs, state

    def init_fun(key, inputs, batched=False, batch_depth=1, **kwargs):
        if(batched == False):
            for i in range(batch_depth):
                inputs = jax.tree_util.tree_map(lambda x: x[None], inputs)

        x = inputs['x']

        # Create the parameters
        axes = tuple(jnp.arange(len(x.shape) - 1))
        params = {'b'    : jnp.mean(x, axis=axes),
                  'log_s': jnp.log(jnp.std(x, axis=axes) + 1e-5)}
        state = {}

        # Keep track of how much to multiply the log_det by
        nonlocal multiply_by
        multiply_by = jnp.prod([s for i, s in enumerate(x.shape) if i >= batch_depth and i < len(x.shape) - 1])

        # Pass the inputs through
        unbatched_inputs = inputs
        for i in range(batch_depth):
            unbatched_inputs = jax.tree_util.tree_map(lambda x: x[0], unbatched_inputs)
        input_shapes = util.tree_shapes(unbatched_inputs)
        input_ndims = util.tree_ndims(unbatched_inputs)

        # Pass the inputs to forward
        vmapped_fun = partial(apply_fun, params, state, **kwargs)
        for i in range(batch_depth):
            vmapped_fun = vmap(vmapped_fun)
        outputs, _ = vmapped_fun(inputs)

        # Need to unbatch in order to get the output shapes
        unbatched_outputs = outputs
        for i in range(batch_depth):
            unbatched_outputs = jax.tree_util.tree_map(lambda x: x[0], unbatched_outputs)
        output_shapes = util.tree_shapes(unbatched_outputs)
        output_ndims = util.tree_ndims(unbatched_outputs)

        if(batched == False):
            outputs = unbatched_outputs

        return outputs, base.Flow(name, input_shapes, output_shapes, input_ndims, output_ndims, params, state, apply_fun)

    return init_fun

# Don't use autobatching!
def BatchNorm(epsilon=1e-5, alpha=0.05, beta_init=jaxinit.zeros, gamma_init=jaxinit.zeros, name='batch_norm'):
    # language=rst
    """
    Invertible batch norm.

    :param axis: Batch axis
    :param epsilon: Constant for numerical stability
    :param alpha: Parameter for exponential moving average of population parameters
    """
    assert 0, 'Haven\'t tested this yet with more than 1 batch.  Use ActNorm instead.'
    expected_dim = None

    def get_bn_params(x, test, running_mean, running_var):
        """ Update the batch norm statistics """
        if(util.is_testing(test)):
            mean, var = running_mean, running_var
        else:
            mean = jnp.mean(x, axis=0)
            var = jnp.var(x, axis=0) + epsilon
            running_mean = (1 - alpha)*running_mean + alpha*mean
            running_var = (1 - alpha)*running_var + alpha*var

        return (mean, var), (running_mean, running_var)

    def apply_fun(params, state, inputs, reverse=False, **kwargs):
        x = inputs['x']
        not_batched = x.ndim == expected_dim
        if(not_batched):
            x = x[None]

        beta, log_gamma = params['beta'], params['log_gamma']
        running_mean, running_var = state['running_mean'], state['running_var']

        # Check if we're training or testing
        test = kwargs.get('test', util.TRAIN)

        # Update the running population parameters
        (mean, var), (running_mean, running_var) = get_bn_params(x, test, running_mean, running_var)

        if(reverse == False):
            x_hat = (x - mean) / jnp.sqrt(var)
            z = jnp.exp(log_gamma)*x_hat + beta
        else:
            x_hat = (x - beta)*jnp.exp(-log_gamma)
            z = x_hat*jnp.sqrt(var) + mean

        log_det = log_gamma.sum()
        log_det += -0.5*jnp.log(var).sum()

        updated_state = {}
        updated_state['running_mean'] = running_mean
        updated_state['running_var'] = running_var

        if(not_batched):
            z = z[0]

        outputs = {'x': z, 'log_det': log_det}

        return outputs, updated_state

    def create_params_and_state(key, input_shapes):
        x_shape = input_shapes['x']
        k1, k2 = random.split(key)
        beta, log_gamma = beta_init(k1, x_shape), gamma_init(k2, x_shape)
        running_mean = jnp.zeros(x_shape)
        running_var = jnp.ones(x_shape)

        nonlocal expected_dim
        expected_dim = len(x_shape)

        params = {'beta': beta,
                  'log_gamma': log_gamma}

        state = {'running_mean': running_mean,
                 'running_var': running_var}

        return params, state

    return base.data_independent_init(name, apply_fun, create_params_and_state)

################################################################################################################

__all__ = ['ActNorm',
           'BatchNorm']
