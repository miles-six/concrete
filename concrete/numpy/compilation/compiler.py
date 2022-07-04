"""
Declaration of `Compiler` class.
"""

import inspect
import os
import traceback
from copy import deepcopy
from enum import Enum, unique
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np

from ..mlir import GraphConverter
from ..representation import Graph
from ..tracing import Tracer
from ..values import Value
from .artifacts import DebugArtifacts
from .circuit import Circuit
from .configuration import Configuration
from .utils import fuse


@unique
class EncryptionStatus(str, Enum):
    """
    EncryptionStatus enum, to represent encryption status of parameters.
    """

    CLEAR = "clear"
    ENCRYPTED = "encrypted"


class Compiler:
    """
    Compiler class, to glue the compilation pipeline.
    """

    function: Callable
    parameter_encryption_statuses: Dict[str, EncryptionStatus]

    configuration: Configuration
    artifacts: Optional[DebugArtifacts]

    inputset: List[Any]
    graph: Optional[Graph]

    def __init__(
        self,
        function: Callable,
        parameter_encryption_statuses: Dict[str, Union[str, EncryptionStatus]],
    ):
        signature = inspect.signature(function)

        missing_args = list(signature.parameters)
        for arg in parameter_encryption_statuses.keys():
            if arg in signature.parameters:
                missing_args.remove(arg)

        if len(missing_args) != 0:
            parameter_str = repr(missing_args[0])
            for arg in missing_args[1:-1]:
                parameter_str += f", {repr(arg)}"
            if len(missing_args) != 1:
                parameter_str += f" and {repr(missing_args[-1])}"

            raise ValueError(
                f"Encryption status{'es' if len(missing_args) > 1 else ''} "
                f"of parameter{'s' if len(missing_args) > 1 else ''} "
                f"{parameter_str} of function '{function.__name__}' "
                f"{'are' if len(missing_args) > 1 else 'is'} not provided"
            )

        additional_args = list(parameter_encryption_statuses)
        for arg in signature.parameters.keys():
            if arg in parameter_encryption_statuses:
                additional_args.remove(arg)

        if len(additional_args) != 0:
            parameter_str = repr(additional_args[0])
            for arg in additional_args[1:-1]:
                parameter_str += f", {repr(arg)}"
            if len(additional_args) != 1:
                parameter_str += f" and {repr(additional_args[-1])}"

            raise ValueError(
                f"Encryption status{'es' if len(additional_args) > 1 else ''} "
                f"of {parameter_str} {'are' if len(additional_args) > 1 else 'is'} provided but "
                f"{'they are' if len(additional_args) > 1 else 'it is'} not a parameter "
                f"of function '{function.__name__}'"
            )

        self.function = function  # type: ignore
        self.parameter_encryption_statuses = {
            param: EncryptionStatus(status.lower())
            for param, status in parameter_encryption_statuses.items()
        }

        self.configuration = Configuration()
        self.artifacts = None

        self.inputset = []
        self.graph = None

    def __call__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Union[
        np.bool_,
        np.integer,
        np.floating,
        np.ndarray,
        Tuple[Union[np.bool_, np.integer, np.floating, np.ndarray], ...],
    ]:
        if len(kwargs) != 0:
            raise RuntimeError(
                f"Calling function '{self.function.__name__}' with kwargs is not supported"
            )

        sample = args[0] if len(args) == 1 else args

        if self.graph is None:
            self._trace(sample)
            assert self.graph is not None

        self.inputset.append(sample)
        return self.graph(*args)

    def _trace(self, sample: Union[Any, Tuple[Any, ...]]):
        """
        Trace the function and fuse the resulting graph with a sample input.

        Args:
            sample (Union[Any, Tuple[Any, ...]]):
                sample to use for tracing
        """

        if self.artifacts is not None:
            self.artifacts.add_source_code(self.function)
            for param, encryption_status in self.parameter_encryption_statuses.items():
                self.artifacts.add_parameter_encryption_status(param, encryption_status)

        parameters = {
            param: Value.of(arg, is_encrypted=(status == EncryptionStatus.ENCRYPTED))
            for arg, (param, status) in zip(
                sample if len(self.parameter_encryption_statuses) > 1 else (sample,),
                self.parameter_encryption_statuses.items(),
            )
        }

        self.graph = Tracer.trace(self.function, parameters)
        if self.artifacts is not None:
            self.artifacts.add_graph("initial", self.graph)

        fuse(self.graph, self.artifacts)

    def _evaluate(
        self,
        action: str,
        inputset: Optional[Union[Iterable[Any], Iterable[Tuple[Any, ...]]]],
    ):
        """
        Trace, fuse, measure bounds, and update values in the resulting graph in one go.

        Args:
            action (str):
                action being performed (e.g., "trace", "compile")

            inputset (Optional[Union[Iterable[Any], Iterable[Tuple[Any, ...]]]]):
                optional inputset to extend accumulated inputset before bounds measurement
        """

        if inputset is not None:
            previous_inputset_length = len(self.inputset)
            for index, sample in enumerate(iter(inputset)):
                self.inputset.append(sample)

                if not isinstance(sample, tuple):
                    sample = (sample,)

                if len(sample) != len(self.parameter_encryption_statuses):
                    self.inputset = self.inputset[:previous_inputset_length]

                    expected = (
                        "a single value"
                        if len(self.parameter_encryption_statuses) == 1
                        else f"a tuple of {len(self.parameter_encryption_statuses)} values"
                    )
                    actual = (
                        "a single value" if len(sample) == 1 else f"a tuple of {len(sample)} values"
                    )

                    raise ValueError(
                        f"Input #{index} of your inputset is not well formed "
                        f"(expected {expected} got {actual})"
                    )

        if self.graph is None:
            try:
                first_sample = next(iter(self.inputset))
            except StopIteration as error:
                raise RuntimeError(
                    f"{action} function '{self.function.__name__}' "
                    f"without an inputset is not supported"
                ) from error

            self._trace(first_sample)
            assert self.graph is not None

        bounds = self.graph.measure_bounds(self.inputset)
        if self.artifacts is not None:
            self.artifacts.add_final_graph_bounds(bounds)

        self.graph.update_with_bounds(bounds)
        if self.artifacts is not None:
            self.artifacts.add_graph("final", self.graph)

    def trace(
        self,
        inputset: Optional[Union[Iterable[Any], Iterable[Tuple[Any, ...]]]] = None,
        configuration: Optional[Configuration] = None,
        artifacts: Optional[DebugArtifacts] = None,
        **kwargs,
    ) -> Graph:
        """
        Trace the function using an inputset.

        Args:
            inputset (Optional[Union[Iterable[Any], Iterable[Tuple[Any, ...]]]]):
                optional inputset to extend accumulated inputset before bounds measurement

            configuration(Optional[Configuration], default = None):
                configuration to use

            artifacts (Optional[DebugArtifacts], default = None):
                artifacts to store information about the process

            kwargs (Dict[str, Any]):
                configuration options to overwrite

        Returns:
            Graph:
                computation graph representing the function prior to MLIR conversion
        """

        old_configuration = deepcopy(self.configuration)
        old_artifacts = deepcopy(self.artifacts)

        if configuration is not None:
            self.configuration = configuration

        if len(kwargs) != 0:
            self.configuration = self.configuration.fork(**kwargs)

        self.artifacts = (
            artifacts
            if artifacts is not None
            else DebugArtifacts()
            if self.configuration.dump_artifacts_on_unexpected_failures
            else None
        )

        try:

            self._evaluate("Tracing", inputset)
            assert self.graph is not None

            if self.configuration.verbose or self.configuration.show_graph:
                graph = self.graph.format()
                longest_line = max([len(line) for line in graph.split("\n")])

                try:  # pragma: no cover

                    # this branch cannot be covered
                    # because `os.get_terminal_size()`
                    # raises an exception during tests

                    columns, _ = os.get_terminal_size()
                    if columns == 0:
                        columns = min(longest_line, 80)
                    else:
                        columns = min(longest_line, columns)
                except OSError:  # pragma: no cover
                    columns = min(longest_line, 80)

                print()

                print("Computation Graph")
                print("-" * columns)
                print(graph)
                print("-" * columns)

                print()

            return self.graph

        except Exception:  # pragma: no cover

            # this branch is reserved for unexpected issues and hence it shouldn't be tested
            # if it could be tested, we would have fixed the underlying issue

            # if the user desires so,
            # we need to export all the information we have about the compilation

            if self.configuration.dump_artifacts_on_unexpected_failures:
                assert self.artifacts is not None
                self.artifacts.export()

                traceback_path = self.artifacts.output_directory.joinpath("traceback.txt")
                with open(traceback_path, "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())

            raise

        finally:

            self.configuration = old_configuration
            self.artifacts = old_artifacts

    # pylint: disable=too-many-branches,too-many-statements

    def compile(
        self,
        inputset: Optional[Union[Iterable[Any], Iterable[Tuple[Any, ...]]]] = None,
        configuration: Optional[Configuration] = None,
        artifacts: Optional[DebugArtifacts] = None,
        **kwargs,
    ) -> Circuit:
        """
        Compile the function using an inputset.

        Args:
            inputset (Optional[Union[Iterable[Any], Iterable[Tuple[Any, ...]]]]):
                optional inputset to extend accumulated inputset before bounds measurement

            configuration(Optional[Configuration], default = None):
                configuration to use

            artifacts (Optional[DebugArtifacts], default = None):
                artifacts to store information about the process

            kwargs (Dict[str, Any]):
                configuration options to overwrite

        Returns:
            Circuit:
                compiled circuit
        """

        old_configuration = deepcopy(self.configuration)
        old_artifacts = deepcopy(self.artifacts)

        if configuration is not None:
            self.configuration = configuration

        if len(kwargs) != 0:
            self.configuration = self.configuration.fork(**kwargs)

        self.artifacts = (
            artifacts
            if artifacts is not None
            else DebugArtifacts()
            if self.configuration.dump_artifacts_on_unexpected_failures
            else None
        )

        try:

            self._evaluate("Compiling", inputset)
            assert self.graph is not None

            mlir = GraphConverter.convert(self.graph, virtual=self.configuration.virtual)
            if self.artifacts is not None:
                self.artifacts.add_mlir_to_compile(mlir)

            if (
                self.configuration.verbose
                or self.configuration.show_graph
                or self.configuration.show_mlir
            ):

                graph = (
                    self.graph.format()
                    if self.configuration.verbose or self.configuration.show_graph
                    else ""
                )

                longest_graph_line = max([len(line) for line in graph.split("\n")])
                longest_mlir_line = max([len(line) for line in mlir.split("\n")])
                longest_line = max(longest_graph_line, longest_mlir_line)

                try:  # pragma: no cover

                    # this branch cannot be covered
                    # because `os.get_terminal_size()`
                    # raises an exception during tests

                    columns, _ = os.get_terminal_size()
                    if columns == 0:
                        columns = min(longest_line, 80)
                    else:
                        columns = min(longest_line, columns)
                except OSError:  # pragma: no cover
                    columns = min(longest_line, 80)

                if self.configuration.verbose or self.configuration.show_graph:
                    print()

                    print("Computation Graph")
                    print("-" * columns)
                    print(graph)
                    print("-" * columns)

                    print()

                if self.configuration.verbose or self.configuration.show_mlir:
                    print(
                        "\n"
                        if not (self.configuration.verbose or self.configuration.show_graph)
                        else "",
                        end="",
                    )

                    print("MLIR")
                    print("-" * columns)
                    print(mlir)
                    print("-" * columns)

                    print()

            circuit = Circuit(self.graph, mlir, self.configuration)
            if not self.configuration.virtual:
                assert circuit.client.specs.client_parameters is not None
                if self.artifacts is not None:
                    self.artifacts.add_client_parameters(
                        circuit.client.specs.client_parameters.serialize()
                    )
            return circuit

        except Exception:  # pragma: no cover

            # this branch is reserved for unexpected issues and hence it shouldn't be tested
            # if it could be tested, we would have fixed the underlying issue

            # if the user desires so,
            # we need to export all the information we have about the compilation

            if self.configuration.dump_artifacts_on_unexpected_failures:
                assert self.artifacts is not None
                self.artifacts.export()

                traceback_path = self.artifacts.output_directory.joinpath("traceback.txt")
                with open(traceback_path, "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())

            raise

        finally:

            self.configuration = old_configuration
            self.artifacts = old_artifacts

    # pylint: enable=too-many-branches,too-many-statements
