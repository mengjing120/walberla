from pystencils import Target
from pystencils.stencil import inverse_direction, offset_to_direction_string


from lbmpy.advanced_streaming import AccessPdfValues, numeric_offsets, numeric_index
from lbmpy.advanced_streaming.indexing import MirroredStencilDirections
from lbmpy.boundaries.boundaryconditions import LbBoundary
from lbmpy.boundaries import ExtrapolationOutflow, FreeSlip, UBB

from pystencils_walberla.additional_data_handler import AdditionalDataHandler


def default_additional_data_handler(boundary_obj: LbBoundary, lb_method, field_name, target=Target.CPU):
    if not boundary_obj.additional_data:
        return None

    if isinstance(boundary_obj, FreeSlip):
        return FreeSlipAdditionalDataHandler(lb_method.stencil, boundary_obj)
    elif isinstance(boundary_obj, UBB):
        return UBBAdditionalDataHandler(lb_method.stencil, boundary_obj)
    elif isinstance(boundary_obj, ExtrapolationOutflow):
        return OutflowAdditionalDataHandler(lb_method.stencil, boundary_obj, target=target, field_name=field_name)
    else:
        raise ValueError(f"No default AdditionalDataHandler available for boundary of type {boundary_obj.__class__}")


class FreeSlipAdditionalDataHandler(AdditionalDataHandler):
    def __init__(self, stencil, boundary_object):
        assert isinstance(boundary_object, FreeSlip)
        self._boundary_object = boundary_object
        super(FreeSlipAdditionalDataHandler, self).__init__(stencil=stencil)

    @property
    def constructor_arguments(self):
        return ""

    @property
    def initialiser_list(self):
        return ""

    @property
    def additional_arguments_for_fill_function(self):
        return ""

    @property
    def additional_parameters_for_fill_function(self):
        return ""

    def data_initialisation(self, direction):
        def array_pattern(dtype, name, content):
            return f"const {str(dtype)} {name} [] = {{ {','.join(str(c) for c in content)} }};"

        offset = self._walberla_stencil[direction]
        inv_offset = inverse_direction(self._walberla_stencil[direction])
        offset_dtype = "int32_t"
        mirror_stencil = MirroredStencilDirections.mirror_stencil

        axis_mirrored = []
        for i, name in enumerate(["x", "y", "z"]):
            mirrored_dir = [self._walberla_stencil.index(mirror_stencil(d, i)) for d in self._walberla_stencil]
            axis_mirrored.append(array_pattern(offset_dtype, f"{name}_axis_mirrored_stencil_dir", mirrored_dir))

        init_list = axis_mirrored[0:self._dim]

        init_list += [f"const Cell n = it.cell() + Cell({offset[0]}, {offset[1]}, {offset[2]});",
                      f"int32_t ref_dir = {self._walberla_stencil.index(inv_offset)}; // dir: {direction}",
                     "element.wnx = 0; // compute discrete normal vector of free slip wall",
                     "element.wny = 0;",
                     f"if( flagField->isPartOfMaskSet( n.x() + {inv_offset[0]}, n.y(), n.z(), domainFlag ) )",
                     "{",
                     f"   element.wnx = {inv_offset[0]};",
                      "   ref_dir = x_axis_mirrored_stencil_dir[ ref_dir ];",
                     "}",
                     f"if( flagField->isPartOfMaskSet( n.x(), n.y() + {inv_offset[1]}, n.z(), domainFlag ) )",
                     "{",
                     f"   element.wny = {inv_offset[1]};",
                      "   ref_dir = y_axis_mirrored_stencil_dir[ ref_dir ];",
                     "}"]
        if self._dim == 3:
            init_list += ["element.wnz = 0;",
                          f"if( flagField->isPartOfMaskSet( n.x(), n.y(), n.z() + {inv_offset[2]}, domainFlag ) )",
                          "{",
                          f"   element.wnz = {inv_offset[2]};",
                          "   ref_dir = z_axis_mirrored_stencil_dir[ ref_dir ];",
                          "}",
                          "// concave corner (neighbors are non-fluid)",
                          "if( element.wnx == 0 && element.wny == 0 && element.wnz == 0 )",
                          "{",
                          f"   element.wnx = {inv_offset[0]};",
                          f"   element.wny = {inv_offset[1]};",
                          f"   element.wnz = {inv_offset[2]};",
                          f"   ref_dir = {direction};",
                          "}"]
        elif self._dim == 2:
            init_list += ["// concave corner (neighbors are non-fluid)",
                          "if( element.wnx == 0 && element.wny == 0 )",
                          "{",
                          f"   element.wnx = {inv_offset[0]};",
                          f"   element.wny = {inv_offset[1]};",
                          f"   ref_dir = {direction};",
                          "}"]
        init_list.append("element.ref_dir = ref_dir;")

        return "\n".join(init_list)

    @property
    def additional_member_variable(self):
        return ""


class UBBAdditionalDataHandler(AdditionalDataHandler):
    def __init__(self, stencil, boundary_object):
        assert isinstance(boundary_object, UBB)
        self._boundary_object = boundary_object
        super(UBBAdditionalDataHandler, self).__init__(stencil=stencil)

    @property
    def constructor_arguments(self):
        return ", std::function<Vector3<real_t>(const Cell &, const shared_ptr<StructuredBlockForest>&, IBlock&)>& " \
               "velocityCallback "

    @property
    def initialiser_list(self):
        return "elementInitaliser(velocityCallback),"

    @property
    def additional_arguments_for_fill_function(self):
        return "blocks, "

    @property
    def additional_parameters_for_fill_function(self):
        return " const shared_ptr<StructuredBlockForest> &blocks, "

    def data_initialisation(self, direction):
        init_list = ["Vector3<real_t> InitialisatonAdditionalData = elementInitaliser(Cell(it.x(), it.y(), it.z()), "
                     "blocks, *block);", "element.vel_0 = InitialisatonAdditionalData[0];",
                     "element.vel_1 = InitialisatonAdditionalData[1];"]
        if self._dim == 3:
            init_list.append("element.vel_2 = InitialisatonAdditionalData[2];")

        return "\n".join(init_list)

    @property
    def additional_member_variable(self):
        return "std::function<Vector3<real_t>(const Cell &, const shared_ptr<StructuredBlockForest>&, IBlock&)> " \
               "elementInitaliser; "


class OutflowAdditionalDataHandler(AdditionalDataHandler):
    def __init__(self, stencil, boundary_object, target=Target.CPU, field_name='pdfs'):
        assert isinstance(boundary_object, ExtrapolationOutflow)
        self._boundary_object = boundary_object
        self._stencil = boundary_object.stencil
        self._lb_method = boundary_object.lb_method
        self._normal_direction = boundary_object.normal_direction
        self._field_name = field_name
        self._target = target
        super(OutflowAdditionalDataHandler, self).__init__(stencil=stencil)

        assert sum([a != 0 for a in self._normal_direction]) == 1,\
            "The outflow boundary is only implemented for straight walls at the moment."

    @property
    def constructor_arguments(self):
        return f", BlockDataID {self._field_name}CPUID_" if self._target == Target.GPU else ""

    @property
    def initialiser_list(self):
        return f"{self._field_name}CPUID({self._field_name}CPUID_)," if self._target == Target.GPU else ""

    @property
    def additional_field_data(self):
        identifier = "CPU" if self._target == Target.GPU else ""
        return f"auto {self._field_name} = block->getData< field::GhostLayerField<real_t, " \
               f"{len(self._stencil)}> >({self._field_name}{identifier}ID); "

    def data_initialisation(self, direction_index):
        pdf_acc = AccessPdfValues(self._boundary_object.stencil,
                                  streaming_pattern=self._boundary_object.streaming_pattern,
                                  timestep=self._boundary_object.zeroth_timestep,
                                  streaming_dir='out')

        init_list = []
        for key, value in self.get_init_dict(pdf_acc, direction_index).items():
            init_list.append(f"element.{key} = {self._field_name}->get({value});")

        return "\n".join(init_list)

    @property
    def additional_member_variable(self):
        return f"BlockDataID {self._field_name}CPUID;"

    @property
    def stencil_info(self):
        stencil_info = []
        for i, d in enumerate(self._stencil):
            if any([a != 0 and b != 0 and a == b for a, b in zip(self._normal_direction, d)]):
                direction = d if self._dim == 3 else d + (0,)
                stencil_info.append((i, direction, ", ".join([str(e) for e in direction])))
        return stencil_info

    def get_init_dict(self, pdf_accessor, direction_index):
        """The Extrapolation Outflow boundary needs additional data. This function provides a list of all values
        which have to be initialised"""
        position = ["it.x()", "it.y()", "it.z()"]
        direction = self._stencil[direction_index]
        inv_dir = self._stencil.index(inverse_direction(direction))

        tangential_offset = tuple(offset - normal for offset, normal in zip(direction, self._normal_direction))

        result = {}
        pos = []
        offsets = numeric_offsets(pdf_accessor.accs[inv_dir])
        for p, o, t in zip(position, offsets, tangential_offset):
            pos.append(p + f" + cell_idx_c({str(o + t)})")
        if self._dim == 2:
            pos.append("0")
        pos.append(str(numeric_index(pdf_accessor.accs[inv_dir])[0]))
        result['pdf'] = ', '.join(pos)

        pos = []
        for p, o, t in zip(position, offsets, tangential_offset):
            pos.append(p + f" + cell_idx_c({str(o + t)})")
        if self._dim == 2:
            pos.append("0")
        pos.append(str(numeric_index(pdf_accessor.accs[inv_dir])[0]))
        result['pdf_nd'] = ', '.join(pos)

        return result
