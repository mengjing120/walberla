import warnings
from collections import OrderedDict, defaultdict
from dataclasses import replace
from itertools import product
from typing import Dict, Optional, Sequence, Tuple

from jinja2 import Environment, PackageLoader, StrictUndefined

from pystencils import Target, CreateKernelConfig
from pystencils import (Assignment, AssignmentCollection, Field, FieldType, create_kernel, create_staggered_kernel)
from pystencils.astnodes import KernelFunction
from pystencils.backends.cbackend import get_headers
from pystencils.backends.simd_instruction_sets import get_supported_instruction_sets
from pystencils.stencil import inverse_direction, offset_to_direction_string

from pystencils.backends.cuda_backend import CudaSympyPrinter
from pystencils.kernelparameters import SHAPE_DTYPE
from pystencils.data_types import TypedSymbol

from pystencils_walberla.jinja_filters import add_pystencils_filters_to_jinja_env
from pystencils_walberla.kernel_selection import KernelCallNode, KernelFamily, HighLevelInterfaceSpec


__all__ = ['generate_sweep', 'generate_pack_info', 'generate_pack_info_for_field', 'generate_pack_info_from_kernel',
           'generate_mpidtype_info_from_kernel', 'KernelInfo',
           'get_vectorize_instruction_set', 'config_from_context', 'generate_selective_sweep']


def generate_sweep(generation_context, class_name, assignments,
                   namespace='pystencils', field_swaps=(), staggered=False, varying_parameters=(),
                   inner_outer_split=False, ghost_layers_to_include=0,
                   target=Target.CPU, data_type=None, cpu_openmp=None, cpu_vectorize_info=None,
                   **create_kernel_params):
    """Generates a waLBerla sweep from a pystencils representation.

    The constructor of the C++ sweep class expects all kernel parameters (fields and parameters) in alphabetical order.
    Fields have to passed using BlockDataID's pointing to walberla fields

    Args:
        generation_context: build system context filled with information from waLBerla's CMake. The context for example
                            defines where to write generated files, if OpenMP is available or which SIMD instruction
                            set should be used. See waLBerla examples on how to get a context.
        class_name: name of the generated sweep class
        assignments: list of assignments defining the stencil update rule or a :class:`KernelFunction`
        namespace: the generated class is accessible as walberla::<namespace>::<class_name>
        field_swaps: sequence of field pairs (field, temporary_field). The generated sweep only gets the first field
                     as argument, creating a temporary field internally which is swapped with the first field after
                     each iteration.
        staggered: set to True to create staggered kernels with `pystencils.create_staggered_kernel`
        varying_parameters: Depending on the configuration, the generated kernels may receive different arguments for
                            different setups. To not have to adapt the C++ application when then parameter change,
                            the varying_parameters sequence can contain parameter names, which are always expected by
                            the C++ class constructor even if the kernel does not need them.
        inner_outer_split: if True generate a sweep that supports separate iteration over inner and outer regions
                           to allow for communication hiding.
        ghost_layers_to_include: determines how many ghost layers should be included for the Sweep.
                                 This is relevant if a setter kernel should also set correct values to the ghost layers.
        target: An pystencils Target to define cpu or gpu code generation. See pystencils.Target
        data_type: default datatype for the kernel creation. Default is double
        cpu_openmp: if loops should use openMP or not.
        cpu_vectorize_info: dictionary containing necessary information for the usage of a SIMD instruction set.
        **create_kernel_params: remaining keyword arguments are passed to `pystencils.create_kernel`
    """
    if staggered:
        assert 'omp_single_loop' not in create_kernel_params
        create_kernel_params['omp_single_loop'] = False
    config = config_from_context(generation_context, target=target, data_type=data_type, cpu_openmp=cpu_openmp,
                                 cpu_vectorize_info=cpu_vectorize_info, **create_kernel_params)

    if isinstance(assignments, KernelFunction):
        ast = assignments
        target = ast.target
    elif not staggered:
        ast = create_kernel(assignments, config=config)
    else:
        # This should not be necessary but create_staggered_kernel does not take a config at the moment ...
        ast = create_staggered_kernel(assignments, **config.__dict__)

    ast.function_name = class_name.lower()

    selection_tree = KernelCallNode(ast)
    generate_selective_sweep(generation_context, class_name, selection_tree, target=target, namespace=namespace,
                             field_swaps=field_swaps, varying_parameters=varying_parameters,
                             inner_outer_split=inner_outer_split, ghost_layers_to_include=ghost_layers_to_include,
                             cpu_vectorize_info=config.cpu_vectorize_info,
                             cpu_openmp=config.cpu_openmp)


def generate_selective_sweep(generation_context, class_name, selection_tree, interface_mappings=(), target=None,
                             namespace='pystencils', field_swaps=(), varying_parameters=(),
                             inner_outer_split=False, ghost_layers_to_include=0,
                             cpu_vectorize_info=None, cpu_openmp=False):
    """Generates a selective sweep from a kernel selection tree. A kernel selection tree consolidates multiple
    pystencils ASTs in a tree-like structure. See also module `pystencils_walberla.kernel_selection`.

    Args:
        generation_context: see documentation of `generate_sweep`
        class_name: name of the generated sweep class
        selection_tree: Instance of `AbstractKernelSelectionNode`, root of the selection tree
        interface_mappings: sequence of `AbstractInterfaceArgumentMapping` instances for selection arguments of
                            the selection tree
        target: `None`, `Target.CPU` or `Target.GPU`; inferred from kernels if `None` is given.
        namespace: see documentation of `generate_sweep`
        field_swaps: see documentation of `generate_sweep`
        varying_parameters: see documentation of `generate_sweep`
        inner_outer_split: see documentation of `generate_sweep`
        ghost_layers_to_include: see documentation of `generate_sweep`
        cpu_vectorize_info: Dictionary containing information about CPU vectorization applied to the kernels
        cpu_openmp: Whether or not CPU kernels use OpenMP parallelization
    """
    def to_name(f):
        return f.name if isinstance(f, Field) else f

    field_swaps = tuple((to_name(e[0]), to_name(e[1])) for e in field_swaps)
    temporary_fields = tuple(e[1] for e in field_swaps)

    kernel_family = KernelFamily(selection_tree, class_name,
                                 temporary_fields, field_swaps, varying_parameters)

    if target is None:
        target = kernel_family.get_ast_attr('target')
    elif target != kernel_family.get_ast_attr('target'):
        raise ValueError('Mismatch between target parameter and AST targets.')

    if not generation_context.cuda and target == Target.GPU:
        return

    representative_field = {p.field_name for p in kernel_family.parameters if p.is_field_parameter}
    representative_field = sorted(representative_field)[0]

    env = Environment(loader=PackageLoader('pystencils_walberla'), undefined=StrictUndefined)
    add_pystencils_filters_to_jinja_env(env)

    interface_spec = HighLevelInterfaceSpec(kernel_family.kernel_selection_parameters, interface_mappings)

    jinja_context = {
        'kernel': kernel_family,
        'namespace': namespace,
        'class_name': class_name,
        'target': target.name.lower(),
        'field': representative_field,
        'ghost_layers_to_include': ghost_layers_to_include,
        'inner_outer_split': inner_outer_split,
        'interface_spec': interface_spec,
        'generate_functor': True,
        'cpu_vectorize_info': cpu_vectorize_info,
        'cpu_openmp': cpu_openmp
    }
    header = env.get_template("Sweep.tmpl.h").render(**jinja_context)
    source = env.get_template("Sweep.tmpl.cpp").render(**jinja_context)

    source_extension = "cpp" if target == Target.CPU else "cu"
    generation_context.write_file(f"{class_name}.h", header)
    generation_context.write_file(f"{class_name}.{source_extension}", source)


def generate_pack_info_for_field(generation_context, class_name: str, field: Field,
                                 direction_subset: Optional[Tuple[Tuple[int, int, int]]] = None,
                                 operator=None, gl_to_inner=False,
                                 target=Target.CPU, data_type=None, cpu_openmp=None,
                                 **create_kernel_params):
    """Creates a pack info for a pystencils field assuming a pull-type stencil, packing all cell elements.

    Args:
        generation_context: see documentation of `generate_sweep`
        class_name: name of the generated class
        field: pystencils field for which to generate pack info
        direction_subset: optional sequence of directions for which values should be packed
                          otherwise a D3Q27 stencil is assumed
        operator: optional operator for, e.g., reduction pack infos
        gl_to_inner: communicates values from ghost layers of sender to interior of receiver
        target: An pystencils Target to define cpu or gpu code generation. See pystencils.Target
        data_type: default datatype for the kernel creation. Default is double
        cpu_openmp: if loops should use openMP or not.
        **create_kernel_params: remaining keyword arguments are passed to `pystencils.create_kernel`
    """

    if not direction_subset:
        direction_subset = tuple((i, j, k) for i, j, k in product(*[(-1, 0, 1)] * 3))

    all_index_accesses = [field(*ind) for ind in product(*[range(s) for s in field.index_shape])]
    return generate_pack_info(generation_context, class_name, {direction_subset: all_index_accesses}, operator=operator,
                              gl_to_inner=gl_to_inner, target=target, data_type=data_type, cpu_openmp=cpu_openmp,
                              **create_kernel_params)


def generate_pack_info_from_kernel(generation_context, class_name: str, assignments: Sequence[Assignment],
                                   kind='pull', operator=None, target=Target.CPU, data_type=None, cpu_openmp=None,
                                   **create_kernel_params):
    """Generates a waLBerla GPU PackInfo from a (pull) kernel.

    Args:
        generation_context: see documentation of `generate_sweep`
        class_name: name of the generated class
        assignments: list of assignments from the compute kernel - generates PackInfo for "pull" part only
                     i.e. the kernel is expected to only write to the center
        kind: can either be pull or push
        operator: optional operator for, e.g., reduction pack infos
        target: An pystencils Target to define cpu or gpu code generation. See pystencils.Target
        data_type: default datatype for the kernel creation. Default is double
        cpu_openmp: if loops should use openMP or not.
        **create_kernel_params: remaining keyword arguments are passed to `pystencils.create_kernel`
    """
    assert kind in ('push', 'pull')
    reads = set()
    writes = set()

    if isinstance(assignments, AssignmentCollection):
        assignments = assignments.all_assignments

    for a in assignments:
        if not isinstance(a, Assignment):
            continue
        reads.update(a.rhs.atoms(Field.Access))
        writes.update(a.lhs.atoms(Field.Access))
    spec = defaultdict(set)
    if kind == 'pull':
        for fa in reads:
            assert all(abs(e) <= 1 for e in fa.offsets)
            if all(offset == 0 for offset in fa.offsets):
                continue
            comm_direction = inverse_direction(fa.offsets)
            for comm_dir in comm_directions(comm_direction):
                spec[(comm_dir,)].add(fa.field.center(*fa.index))
    elif kind == 'push':
        for fa in writes:
            assert all(abs(e) <= 1 for e in fa.offsets)
            if all(offset == 0 for offset in fa.offsets):
                continue
            for comm_dir in comm_directions(fa.offsets):
                spec[(comm_dir,)].add(fa)
    else:
        raise ValueError("Invalid 'kind' parameter")
    return generate_pack_info(generation_context, class_name, spec, operator=operator,
                              target=target, data_type=data_type, cpu_openmp=cpu_openmp, **create_kernel_params)


def generate_pack_info(generation_context, class_name: str,
                       directions_to_pack_terms: Dict[Tuple[Tuple], Sequence[Field.Access]],
                       namespace='pystencils', operator=None, gl_to_inner=False,
                       target=Target.CPU, data_type=None, cpu_openmp=None,
                       **create_kernel_params):
    """Generates a waLBerla GPU PackInfo

    Args:
        generation_context: see documentation of `generate_sweep`
        class_name: name of the generated class
        directions_to_pack_terms: maps tuples of directions to read field accesses, specifying which values have to be
                                  packed for which direction
        namespace: inner namespace of the generated class
        operator: optional operator for, e.g., reduction pack infos
        gl_to_inner: communicates values from ghost layers of sender to interior of receiver
        target: An pystencils Target to define cpu or gpu code generation. See pystencils.Target
        data_type: default datatype for the kernel creation. Default is double
        cpu_openmp: if loops should use openMP or not.
        **create_kernel_params: remaining keyword arguments are passed to `pystencils.create_kernel`
    """
    items = [(e[0], sorted(e[1], key=lambda x: str(x))) for e in directions_to_pack_terms.items()]
    items = sorted(items, key=lambda e: e[0])
    directions_to_pack_terms = OrderedDict(items)

    config = config_from_context(generation_context, target=target, data_type=data_type, cpu_openmp=cpu_openmp,
                                 **create_kernel_params)

    config_zero_gl = config_from_context(generation_context, target=target, data_type=data_type, cpu_openmp=cpu_openmp,
                                         ghost_layers=0, **create_kernel_params)

    # Vectorisation of the pack info is not implemented.
    config = replace(config, cpu_vectorize_info=None)
    config_zero_gl = replace(config_zero_gl, cpu_vectorize_info=None)

    template_name = "CpuPackInfo.tmpl" if config.target == Target.CPU else 'GpuPackInfo.tmpl'

    fields_accessed = set()
    for terms in directions_to_pack_terms.values():
        for term in terms:
            assert isinstance(term, Field.Access)  # and all(e == 0 for e in term.offsets)
            fields_accessed.add(term)

    field_names = {fa.field.name for fa in fields_accessed}

    data_types = {fa.field.dtype for fa in fields_accessed}
    if len(data_types) == 0:
        raise ValueError("No fields to pack!")
    if len(data_types) != 1:
        err_detail = "\n".join(" - {} [{}]".format(f.name, f.dtype) for f in fields_accessed)
        raise NotImplementedError("Fields of different data types are used - this is not supported.\n" + err_detail)
    dtype = data_types.pop()

    pack_kernels = OrderedDict()
    unpack_kernels = OrderedDict()
    all_accesses = set()
    elements_per_cell = OrderedDict()
    for direction_set, terms in directions_to_pack_terms.items():
        for d in direction_set:
            if not all(abs(i) <= 1 for i in d):
                raise NotImplementedError("Only first neighborhood supported")

        buffer = Field.create_generic('buffer', spatial_dimensions=1, field_type=FieldType.BUFFER,
                                      dtype=dtype.numpy_dtype, index_shape=(len(terms),))

        direction_strings = tuple(offset_to_direction_string(d) for d in direction_set)
        all_accesses.update(terms)

        pack_assignments = [Assignment(buffer(i), term) for i, term in enumerate(terms)]
        pack_ast = create_kernel(pack_assignments, config=config_zero_gl)
        pack_ast.function_name = 'pack_{}'.format("_".join(direction_strings))
        if operator is None:
            unpack_assignments = [Assignment(term, buffer(i)) for i, term in enumerate(terms)]
        else:
            unpack_assignments = [Assignment(term, operator(term, buffer(i))) for i, term in enumerate(terms)]
        unpack_ast = create_kernel(unpack_assignments, config=config_zero_gl)
        unpack_ast.function_name = 'unpack_{}'.format("_".join(direction_strings))

        pack_kernels[direction_strings] = KernelInfo(pack_ast)
        unpack_kernels[direction_strings] = KernelInfo(unpack_ast)
        elements_per_cell[direction_strings] = len(terms)
    fused_kernel = create_kernel([Assignment(buffer.center, t) for t in all_accesses], config=config)

    jinja_context = {
        'class_name': class_name,
        'pack_kernels': pack_kernels,
        'unpack_kernels': unpack_kernels,
        'fused_kernel': KernelInfo(fused_kernel),
        'elements_per_cell': elements_per_cell,
        'headers': get_headers(fused_kernel),
        'target': config.target.name.lower(),
        'dtype': dtype,
        'field_name': field_names.pop(),
        'namespace': namespace,
        'gl_to_inner': gl_to_inner,
    }
    env = Environment(loader=PackageLoader('pystencils_walberla'), undefined=StrictUndefined)
    add_pystencils_filters_to_jinja_env(env)
    header = env.get_template(template_name + ".h").render(**jinja_context)
    source = env.get_template(template_name + ".cpp").render(**jinja_context)

    source_extension = "cpp" if config.target == Target.CPU else "cu"
    generation_context.write_file(f"{class_name}.h", header)
    generation_context.write_file(f"{class_name}.{source_extension}", source)


def generate_mpidtype_info_from_kernel(generation_context, class_name: str,
                                       assignments: Sequence[Assignment], kind='pull', namespace='pystencils'):
    assert kind in ('push', 'pull')
    reads = set()
    writes = set()

    if isinstance(assignments, AssignmentCollection):
        assignments = assignments.all_assignments

    for a in assignments:
        if not isinstance(a, Assignment):
            continue
        reads.update(a.rhs.atoms(Field.Access))
        writes.update(a.lhs.atoms(Field.Access))

    spec = defaultdict(set)
    if kind == 'pull':
        read_fields = set(fa.field for fa in reads)
        assert len(read_fields) == 1, "Only scenarios where one fields neighbors are accessed"
        field = read_fields.pop()
        for fa in reads:
            assert all(abs(e) <= 1 for e in fa.offsets)
            if all(offset == 0 for offset in fa.offsets):
                continue
            comm_direction = inverse_direction(fa.offsets)
            for comm_dir in comm_directions(comm_direction):
                assert len(fa.index) == 1, "Supports only fields with a single index dimension"
                spec[(offset_to_direction_string(comm_dir),)].add(fa.index[0])
    elif kind == 'push':
        written_fields = set(fa.field for fa in writes)
        assert len(written_fields) == 1, "Only scenarios where one fields neighbors are accessed"
        field = written_fields.pop()

        for fa in writes:
            assert all(abs(e) <= 1 for e in fa.offsets)
            if all(offset == 0 for offset in fa.offsets):
                continue
            for comm_dir in comm_directions(fa.offsets):
                assert len(fa.index) == 1, "Supports only fields with a single index dimension"
                spec[(offset_to_direction_string(comm_dir),)].add(fa.index[0])
    else:
        raise ValueError("Invalid 'kind' parameter")

    jinja_context = {
        'class_name': class_name,
        'namespace': namespace,
        'kind': kind,
        'field_name': field.name,
        'f_size': field.index_shape[0],
        'spec': spec,
    }
    env = Environment(loader=PackageLoader('pystencils_walberla'), undefined=StrictUndefined)
    header = env.get_template("MpiDtypeInfo.tmpl.h").render(**jinja_context)
    generation_context.write_file(f"{class_name}.h", header)


# ---------------------------------- Internal --------------------------------------------------------------------------


class KernelInfo:
    def __init__(self, ast, temporary_fields=(), field_swaps=(), varying_parameters=()):
        self.ast = ast
        self.temporary_fields = tuple(temporary_fields)
        self.field_swaps = tuple(field_swaps)
        self.varying_parameters = tuple(varying_parameters)
        self.parameters = ast.get_parameters()  # cache parameters here

    @property
    def fields_accessed(self):
        return self.ast.fields_accessed

    def get_ast_attr(self, name):
        """Returns the value of an attribute of the AST managed by this KernelInfo.
        For compatibility with KernelFamily."""
        return self.ast.__getattribute__(name)

    def generate_kernel_invocation_code(self, **kwargs):
        ast = self.ast
        ast_params = self.parameters
        is_cpu = self.ast.target == Target.CPU
        call_parameters = ", ".join([p.symbol.name for p in ast_params])

        if not is_cpu:
            stream = kwargs.get('stream', '0')
            spatial_shape_symbols = kwargs.get('spatial_shape_symbols', ())

            if not spatial_shape_symbols:
                spatial_shape_symbols = [p.symbol for p in ast_params if p.is_field_shape]
                spatial_shape_symbols.sort(key=lambda e: e.coordinate)
            else:
                spatial_shape_symbols = [TypedSymbol(s, SHAPE_DTYPE) for s in spatial_shape_symbols]

            assert spatial_shape_symbols, "No shape parameters in kernel function arguments.\n"\
                "Please only use kernels for generic field sizes!"

            indexing_dict = ast.indexing.call_parameters(spatial_shape_symbols)
            sp_printer_c = CudaSympyPrinter()
            kernel_call_lines = [
                "dim3 _block(int(%s), int(%s), int(%s));" % tuple(sp_printer_c.doprint(e)
                                                                  for e in indexing_dict['block']),
                "dim3 _grid(int(%s), int(%s), int(%s));" % tuple(sp_printer_c.doprint(e)
                                                                 for e in indexing_dict['grid']),
                "internal_%s::%s<<<_grid, _block, 0, %s>>>(%s);" % (ast.function_name, ast.function_name,
                                                                    stream, call_parameters),
            ]

            return "\n".join(kernel_call_lines)
        else:
            return f"internal_{ast.function_name}::{ast.function_name}({call_parameters});"


def get_vectorize_instruction_set(generation_context):
    if generation_context.optimize_for_localhost:
        supported_instruction_sets = get_supported_instruction_sets()
        if supported_instruction_sets:
            return supported_instruction_sets[-1]
        else:  # if cpuinfo package is not installed
            warnings.warn("Could not obtain supported vectorization instruction sets - defaulting to sse. "
                          "This problem can probably be fixed by installing py-cpuinfo. This package can "
                          "gather the needed hardware information.")
            return 'sse'
    else:
        return None


def config_from_context(generation_context, target=Target.CPU, data_type=None,
                        cpu_openmp=None, cpu_vectorize_info=None, **kwargs):

    if target == Target.GPU and not generation_context.cuda:
        raise ValueError("can not generate cuda code if waLBerla is not build with CUDA. Please use "
                         "-DWALBERLA_BUILD_WITH_CUDA=1 for configuring cmake")

    default_dtype = "float64" if generation_context.double_accuracy else "float32"
    if data_type is None:
        data_type = default_dtype

    if cpu_openmp and not generation_context.openmp:
        warnings.warn("Code is generated with OpenMP pragmas but waLBerla is not build with OpenMP. "
                      "The compilation might not work due to wrong compiler flags. "
                      "Please use -DWALBERLA_BUILD_WITH_OPENMP=1 for configuring cmake")

    if cpu_openmp is None:
        cpu_openmp = generation_context.openmp

    if cpu_vectorize_info is None:
        cpu_vectorize_info = {}

    default_vec_is = get_vectorize_instruction_set(generation_context)

    cpu_vectorize_info['instruction_set'] = cpu_vectorize_info.get('instruction_set', default_vec_is)
    cpu_vectorize_info['assume_inner_stride_one'] = cpu_vectorize_info.get('assume_inner_stride_one', True)
    cpu_vectorize_info['assume_aligned'] = cpu_vectorize_info.get('assume_aligned', False)
    cpu_vectorize_info['nontemporal'] = cpu_vectorize_info.get('nontemporal', False)

    config = CreateKernelConfig(target=target, data_type=data_type,
                                cpu_openmp=cpu_openmp, cpu_vectorize_info=cpu_vectorize_info,
                                **kwargs)

    return config


def comm_directions(direction):
    if all(e == 0 for e in direction):
        yield direction
    binary_numbers_list = binary_numbers(len(direction))
    for comm_direction in binary_numbers_list:
        for i in range(len(direction)):
            if direction[i] == 0:
                comm_direction[i] = 0
            if direction[i] == -1 and comm_direction[i] == 1:
                comm_direction[i] = -1
        if not all(e == 0 for e in comm_direction):
            yield tuple(comm_direction)


def binary_numbers(n):
    result = list()
    for i in range(1 << n):
        binary_number = bin(i)[2:]
        binary_number = '0' * (n - len(binary_number)) + binary_number
        result.append((list(map(int, binary_number))))
    return result
