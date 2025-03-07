# Copyright 2018-2022 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains functions for adding the JAX interface
to a PennyLane Device class.
"""
# pylint: disable=too-many-arguments
import jax
import jax.numpy as jnp

import pennylane as qml
from pennylane.interfaces import InterfaceUnsupportedError
from pennylane.measurements import CountsMP, ProbabilityMP, SampleMP
from pennylane.transforms import convert_to_numpy_parameters

dtype = jnp.float64


def _set_copy_and_unwrap_tape(t, a, unwrap=True):
    """Copy a given tape with operations and set parameters"""
    tc = t.bind_new_parameters(a, t.trainable_params)
    return convert_to_numpy_parameters(tc) if unwrap else tc


def set_parameters_on_copy_and_unwrap(tapes, params, unwrap=True):
    """Copy a set of tapes with operations and set parameters"""
    return tuple(_set_copy_and_unwrap_tape(t, a, unwrap=unwrap) for t, a in zip(tapes, params))


def get_jax_interface_name(tapes):
    """Check all parameters in each tape and output the name of the suitable
    JAX interface.

    This function checks each tape and determines if any of the gate parameters
    was transformed by a JAX transform such as ``jax.jit``. If so, it outputs
    the name of the JAX interface with jit support.

    Note that determining if jit support should be turned on is done by
    checking if parameters are abstract. Parameters can be abstract not just
    for ``jax.jit``, but for other JAX transforms (vmap, pmap, etc.) too. The
    reason is that JAX doesn't have a public API for checking whether or not
    the execution is within the jit transform.

    Args:
        tapes (Sequence[.QuantumTape]): batch of tapes to execute

    Returns:
        str: name of JAX interface that fits the tape parameters, "jax" or
        "jax-jit"
    """
    for t in tapes:
        for op in t:
            # Unwrap the observable from a MeasurementProcess
            op = op.obs if hasattr(op, "obs") else op
            if op is not None:
                # Some MeasurementProcess objects have op.obs=None
                for param in op.data:
                    if qml.math.is_abstract(param):
                        return "jax-jit"

    return "jax"


def execute_legacy(
    tapes, device, execute_fn, gradient_fn, gradient_kwargs, _n=1, max_diff=1, mode=None
):
    """Execute a batch of tapes with JAX parameters on a device.

    Args:
        tapes (Sequence[.QuantumTape]): batch of tapes to execute
        device (pennylane.Device): Device to use to execute the batch of tapes.
            If the device does not provide a ``batch_execute`` method,
            by default the tapes will be executed in serial.
        execute_fn (callable): The execution function used to execute the tapes
            during the forward pass. This function must return a tuple ``(results, jacobians)``.
            If ``jacobians`` is an empty list, then ``gradient_fn`` is used to
            compute the gradients during the backwards pass.
        gradient_kwargs (dict): dictionary of keyword arguments to pass when
            determining the gradients of tapes
        gradient_fn (callable): the gradient function to use to compute quantum gradients
        _n (int): a positive integer used to track nesting of derivatives, for example
            if the nth-order derivative is requested.
        max_diff (int): If ``gradient_fn`` is a gradient transform, this option specifies
            the maximum order of derivatives to support. Increasing this value allows
            for higher order derivatives to be extracted, at the cost of additional
            (classical) computational overhead during the backwards pass.
        mode (str): Whether the gradients should be computed on the forward
            pass (``forward``) or the backward pass (``backward``).

    Returns:
        list[list[float]]: A nested list of tape results. Each element in
        the returned list corresponds in order to the provided tapes.
    """
    # pylint: disable=unused-argument
    if max_diff > 1:
        raise InterfaceUnsupportedError("The JAX interface only supports first order derivatives.")

    _validate_tapes(tapes)

    if _n == 1:
        for tape in tapes:
            # set the trainable parameters
            params = tape.get_parameters(trainable_only=False)
            tape.trainable_params = qml.math.get_trainable_indices(params)

    parameters = tuple(list(t.get_parameters()) for t in tapes)

    if gradient_fn is None:
        return _execute_fwd_legacy(
            parameters,
            tapes=tapes,
            device=device,
            execute_fn=execute_fn,
            gradient_kwargs=gradient_kwargs,
            _n=_n,
        )

    return _execute_legacy(
        parameters,
        tapes=tapes,
        device=device,
        execute_fn=execute_fn,
        gradient_fn=gradient_fn,
        gradient_kwargs=gradient_kwargs,
        _n=_n,
    )


def _validate_tapes(tapes):
    """Validates that the input tapes are compatible with JAX support.

    Note: the goal of this validation is to filter out cases where ragged
    outputs for QNodes may arise. Such QNodes involve creating arrays from
    ragged nested sequences that can not be handled by JAX.

    Raises:
        InterfaceUnsupportedError: if tapes that produce ragged outputs were provided
    """
    for t in tapes:
        measurement_types = [type(m) for m in t.measurements]
        set_of_measurement_types = set(measurement_types)
        probs_or_sample_measure = (
            SampleMP in measurement_types or ProbabilityMP in measurement_types
        )
        if probs_or_sample_measure and len(set_of_measurement_types) > 1:
            raise InterfaceUnsupportedError(
                "Using the JAX interface, sample and probability measurements cannot be mixed with other measurement types."
            )

        if ProbabilityMP in measurement_types:
            set_len_wires = {len(m.wires) for m in t.measurements}
            if len(set_len_wires) > 1:
                raise InterfaceUnsupportedError(
                    "Using the JAX interface, multiple probability measurements need to have the same number of wires specified."
                )


def _execute_legacy(
    params,
    tapes=None,
    device=None,
    execute_fn=None,
    gradient_fn=None,
    gradient_kwargs=None,
    _n=1,
):  # pylint: disable=dangerous-default-value,unused-argument
    """The main interface execution function where jacobians of the execute
    function are computed by the registered backward function."""

    def array_if_not_counts(tape, r):
        """Auxiliary function to convert the result of a tape to an array,
        unless the tape had Counts measurements that are represented with
        dictionaries. JAX NumPy arrays don't support dictionaries."""
        if any(isinstance(m, CountsMP) for m in tape.measurements):
            if tape.batch_size is not None:
                raise InterfaceUnsupportedError(
                    "Broadcasted circuits with counts return types are only supported with "
                    "the new return system. Use qml.enable_return() to turn it on."
                )
            return r
        return jnp.array(r)

    @jax.custom_vjp
    def wrapped_exec(params):
        new_tapes = set_parameters_on_copy_and_unwrap(tapes, params)
        res, _ = execute_fn(new_tapes, **gradient_kwargs)

        if len(tapes) > 1:
            res = [array_if_not_counts(tape, r) for tape, r in zip(tapes, res)]
        else:
            res = array_if_not_counts(tapes[0], res)

        return res

    def wrapped_exec_fwd(params):
        return wrapped_exec(params), params

    def wrapped_exec_bwd(params, g):
        if isinstance(gradient_fn, qml.gradients.gradient_transform):
            new_tapes = set_parameters_on_copy_and_unwrap(tapes, params)
            vjp_tapes, processing_fn = qml.gradients.batch_vjp(
                new_tapes,
                g,
                gradient_fn,
                reduction="append",
                gradient_kwargs=gradient_kwargs,
            )

            partial_res = execute_fn(vjp_tapes)[0]

            for t in tapes:
                multi_probs = (
                    any(isinstance(m, ProbabilityMP) for m in t.measurements)
                    and len(t.measurements) > 1
                )

            if multi_probs:
                # For multiple probability measurements, adjust the
                # rows/columns in the result to match other interfaces
                new_partial_res = []
                for r in partial_res:
                    if r.ndim > 1:
                        new_partial_res.append(r.swapaxes(0, 1))
                    else:
                        new_partial_res.append(r)
                partial_res = new_partial_res

            res = processing_fn(partial_res)
            vjps = jnp.concatenate(res)

            param_idx = 0
            res = []

            # Group the vjps based on the parameters of the tapes
            for p in params:
                param_vjp = vjps[param_idx : param_idx + len(p)]
                res.append(param_vjp)
                param_idx += len(p)

            # Unstack partial results into ndim=0 arrays to allow
            # differentiability with JAX
            # E.g.,
            # [Array([-0.9553365], dtype=float32), Array([0., 0.],
            # dtype=float32)]
            # is mapped to
            # [[Array(-0.9553365, dtype=float32)], [Array(0.,
            # dtype=float32), Array(0., dtype=float32)]].
            need_unstacking = any(r.ndim != 0 for r in res)
            if need_unstacking:
                res = [qml.math.unstack(x) for x in res]

            return (tuple(res),)

        # Gradient function is a device method.
        unwrapped_tapes = tuple(convert_to_numpy_parameters(t) for t in tapes)
        jacs = gradient_fn(unwrapped_tapes, **gradient_kwargs)

        vjps = [qml.gradients.compute_vjp(d, jac) for d, jac in zip(g, jacs)]
        res = [[jnp.array(p) for p in v] for v in vjps]
        return (tuple(res),)

    wrapped_exec.defvjp(wrapped_exec_fwd, wrapped_exec_bwd)
    return wrapped_exec(params)


def _raise_vector_valued_fwd(tapes):
    """Raises an error for vector-valued tapes in forward mode due to incorrect
    results being produced.

    There is an issue when jax.jacobian is being used, either due to issues
    with tensor updating (TypeError: Updates tensor must be of rank 0; got 1)
    or because jax.vmap introduces a redundant dimensionality in the result by
    duplicating entries.

    Example to the latter:

    1. Output when using jax.jacobian:
    Array([[-0.09983342,  0.01983384],\n
                 [-0.09983342, 0.01983384]], dtype=float64),
    Array([[ 0.        , -0.97517033],\n
                 [ 0.        , -0.97517033]], dtype=float64)),

    2. Expected output:
    Array([[-0.09983342, 0.01983384],\n
                [ 0.        , -0.97517033]]

    The output produced by this function matches 1.
    """
    scalar_outputs = all(t.output_dim == 1 for t in tapes)
    if not scalar_outputs:
        raise InterfaceUnsupportedError(
            "Computing the jacobian of vector-valued tapes is not supported currently in forward mode."
        )


def _execute_fwd_legacy(
    params,
    tapes=None,
    device=None,
    execute_fn=None,
    gradient_kwargs=None,
    _n=1,
):  # pylint: disable=dangerous-default-value,unused-argument
    """The auxiliary execute function for cases when the user requested
    jacobians to be computed in forward mode or when no gradient function was
    provided."""

    @jax.custom_vjp
    def wrapped_exec(params):
        new_tapes = set_parameters_on_copy_and_unwrap(tapes, params)
        res, jacs = execute_fn(new_tapes, **gradient_kwargs)

        if len(tapes) > 1:
            res, jacs = [jnp.array(r) for r in res], [jnp.array(j) for j in jacs]
        else:
            res, jacs = jnp.array(res), jnp.array(jacs)
        return res, jacs

    def wrapped_exec_fwd(params):
        res, jacs = wrapped_exec(params)
        return res, tuple([jacs, params])

    def wrapped_exec_bwd(params, g):
        # Use the jacobian that was computed on the forward pass
        jacs, params = params

        _raise_vector_valued_fwd(tapes)

        # Adjust the structure of how the jacobian is returned to match the
        # non-forward mode cases
        # E.g.,
        # [Array([[ 0.06695931,  0.01383095, -0.46500877]], dtype=float32)]
        # is mapped to
        # [[Array(0.06695931, dtype=float32), Array(0.01383095,
        # dtype=float32), Array(-0.46500877, dtype=float32)]]
        res_jacs = []
        for j in jacs:
            this_j = []
            for i in range(j.shape[1]):
                arr = (
                    j[0, i] if j.shape[0] == 1 else jnp.array([j[k, i] for k in range(j.shape[0])])
                )
                this_j.append(arr)
            res_jacs.append(this_j)
        return tuple([tuple(res_jacs)])

    wrapped_exec.defvjp(wrapped_exec_fwd, wrapped_exec_bwd)
    res = wrapped_exec(params)

    tracing = any(isinstance(r, jax.interpreters.ad.JVPTracer) for r in res)

    # When there are no tracers (not differentiating), we have the result of
    # the forward pass and the jacobian, but only need the result of the
    # forward pass
    if len(res) == 2 and not tracing:
        res = res[0]

    return res


def execute(tapes, device, execute_fn, gradient_fn, gradient_kwargs, _n=1, max_diff=2):
    """Execute a batch of tapes with JAX parameters on a device.

    Args:
        tapes (Sequence[.QuantumTape]): batch of tapes to execute
        device (pennylane.Device): Device to use for the shots vectors.
        execute_fn (callable): The execution function used to execute the tapes
            during the forward pass. This function must return a tuple ``(results, jacobians)``.
            If ``jacobians`` is an empty list, then ``gradient_fn`` is used to
            compute the gradients during the backwards pass.
        gradient_kwargs (dict): dictionary of keyword arguments to pass when
            determining the gradients of tapes
        gradient_fn (callable): the gradient function to use to compute quantum gradients
        _n (int): a positive integer used to track nesting of derivatives, for example
            if the nth-order derivative is requested.
        max_diff (int): If ``gradient_fn`` is a gradient transform, this option specifies
            the maximum order of derivatives to support. Increasing this value allows
            for higher order derivatives to be extracted, at the cost of additional
            (classical) computational overhead during the backwards pass.

    Returns:
        list[list[float]]: A nested list of tape results. Each element in
        the returned list corresponds in order to the provided tapes.
    """
    # Set the trainable parameters
    if _n == 1:
        for tape in tapes:
            params = tape.get_parameters(trainable_only=False)
            tape.trainable_params = qml.math.get_trainable_indices(params)

    parameters = tuple(list(t.get_parameters()) for t in tapes)

    if gradient_fn is None:
        # PennyLane forward execution
        return _execute_fwd(
            parameters,
            tapes,
            execute_fn,
            gradient_kwargs,
            _n=_n,
        )

    # PennyLane backward execution
    return _execute_bwd(
        parameters,
        tapes,
        device,
        execute_fn,
        gradient_fn,
        gradient_kwargs,
        _n=_n,
        max_diff=max_diff,
    )


def _execute_bwd(
    params,
    tapes,
    device,
    execute_fn,
    gradient_fn,
    gradient_kwargs,
    _n=1,
    max_diff=2,
):
    """The main interface execution function where jacobians of the execute
    function are computed by the registered backward function."""

    # pylint: disable=unused-variable
    # Copy a given tape with operations and set parameters

    if isinstance(device, qml.devices.experimental.Device):
        # cant test until we integrate device with shot vector
        has_partitioned_shots = tapes[0].shots.has_partitioned_shots
        jvp_shots = None
    else:
        has_partitioned_shots = jvp_shots = device.shot_vector

    @jax.custom_jvp
    def execute_wrapper(params):
        new_tapes = set_parameters_on_copy_and_unwrap(tapes, params)
        res, _ = execute_fn(new_tapes, **gradient_kwargs)
        return _to_jax_shot_vector(res) if has_partitioned_shots else _to_jax(res)

    @execute_wrapper.defjvp
    def execute_wrapper_jvp(primals, tangents):
        """Primals[0] are parameters as Jax tracers and tangents[0] is a list of tangent vectors as Jax tracers."""
        if isinstance(gradient_fn, qml.gradients.gradient_transform):
            at_max_diff = _n == max_diff
            new_tapes = set_parameters_on_copy_and_unwrap(tapes, primals[0], unwrap=at_max_diff)
            _args = (
                new_tapes,
                tangents[0],
                gradient_fn,
                jvp_shots,
            )
            _kwargs = {
                "reduction": "append",
                "gradient_kwargs": gradient_kwargs,
            }
            if at_max_diff:
                jvp_tapes, processing_fn = qml.gradients.batch_jvp(*_args, **_kwargs)
                jvps = processing_fn(execute_fn(jvp_tapes)[0])
            else:
                jvp_tapes, processing_fn = qml.gradients.batch_jvp(*_args, **_kwargs)

                jvps = processing_fn(
                    execute(
                        jvp_tapes,
                        device,
                        execute_fn,
                        gradient_fn,
                        gradient_kwargs,
                        _n=_n + 1,
                        max_diff=max_diff,
                    )
                )
            res = execute_wrapper(primals[0])
        else:
            # Execution: execute the function first
            res = execute_wrapper(primals[0])
            # Backward: Gradient function is a device method.
            new_tapes = set_parameters_on_copy_and_unwrap(tapes, primals[0])
            jacs = gradient_fn(new_tapes, **gradient_kwargs)
            multi_measurements = [len(tape.measurements) > 1 for tape in new_tapes]
            jvps = _compute_jvps(jacs, tangents[0], multi_measurements)

        return res, jvps

    return execute_wrapper(params)


def _execute_fwd(
    params,
    tapes,
    execute_fn,
    gradient_kwargs,
    _n=1,
):
    """The auxiliary execute function for cases when the user requested
    jacobians to be computed in forward mode (e.g. adjoint) or when no gradient function was
    provided. This function does not allow multiple derivatives. It currently does not support shot vectors
    because adjoint jacobian for default qubit does not support it.."""

    # pylint: disable=unused-variable
    @jax.custom_jvp
    def execute_wrapper(params):
        new_tapes = set_parameters_on_copy_and_unwrap(tapes, params)
        res, jacs = execute_fn(new_tapes, **gradient_kwargs)
        res = _to_jax(res)

        return res, jacs

    @execute_wrapper.defjvp
    def execute_wrapper_jvp(primals, tangents):
        """Primals[0] are parameters as Jax tracers and tangents[0] is a list of tangent vectors as Jax tracers."""
        res, jacs = execute_wrapper(primals[0])
        multi_measurements = [len(tape.measurements) > 1 for tape in tapes]

        jvps = _compute_jvps(jacs, tangents[0], multi_measurements)
        return (res, jacs), (jvps, jacs)

    res, _jacs = execute_wrapper(params)
    return res


def _compute_jvps(jacs, tangents, multi_measurements):
    """Compute the jvps of multiple tapes, directly for a Jacobian and tangents."""
    jvps = []
    for i, multi in enumerate(multi_measurements):
        compute_func = (
            qml.gradients.compute_jvp_multi if multi else qml.gradients.compute_jvp_single
        )
        jvps.append(compute_func(tangents[i], jacs[i]))
    return tuple(jvps)


def _is_count_result(r):
    """Checks if ``r`` is a single count (or broadcasted count) result"""
    return isinstance(r, dict) or isinstance(r, list) and all(isinstance(i, dict) for i in r)


def _to_jax(res):
    """From a list of tapes results (each result is either a np.array or tuple), transform it to a list of Jax
    results (structure stay the same)."""
    res_ = []
    for r in res:
        if _is_count_result(r):
            res_.append(r)
        elif not isinstance(r, tuple):
            res_.append(jnp.array(r))
        else:
            sub_r = []
            for r_i in r:
                if _is_count_result(r_i):
                    sub_r.append(r_i)
                else:
                    sub_r.append(jnp.array(r_i))
            res_.append(tuple(sub_r))
    return tuple(res_)


def _to_jax_shot_vector(res):
    """Convert the results obtained by executing a list of tapes on a device with a shot vector to JAX objects while preserving the input structure.

    The expected structure of the inputs is a list of tape results with each element in the list being a tuple due to execution using shot vectors.
    """
    return tuple(tuple(_to_jax([r_])[0] for r_ in r) for r in res)
