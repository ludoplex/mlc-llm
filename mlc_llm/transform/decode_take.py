"""Fusing and inlining decode function into embedding table lookup."""
import tvm
from tvm import relax, tir
from tvm.ir.module import IRModule
from tvm.relax.dpl.pattern import GlobalVarPattern, TuplePattern, is_op, wildcard


def pattern_check(ctx: relax.transform.PatternCheckContext) -> bool:
    decode = ctx.annotated_expr["decode"]
    if not isinstance(decode, relax.expr.Call):
        return False
    take = ctx.annotated_expr["take"]
    return (
        False
        if not isinstance(take.args[0], relax.GlobalVar)
        or not isinstance(decode.args[0], relax.GlobalVar)
        else "take" in take.args[0].name_hint
        and "decode" in decode.args[0].name_hint
    )


def decode_take_pattern(n_aux_tensor: int):
    aux_tensors = [wildcard(), wildcard(), wildcard()]
    decode = is_op("relax.call_tir")(
        GlobalVarPattern(),
        TuplePattern([*aux_tensors[:n_aux_tensor]]),
        add_constraint=False,
    )
    indices = wildcard()
    take_args = [decode, indices]
    take = is_op("relax.call_tir")(
        GlobalVarPattern(), TuplePattern(take_args), add_constraint=False
    )

    annotations = {
        "take": take,
        "decode": decode,
        "indices": indices,
    }

    return take, annotations, pattern_check


@tvm.transform.module_pass(opt_level=0, name="FuseDecodeTake")
class FuseDecodeTake:
    def transform_module(
        self, mod: IRModule, ctx: tvm.transform.PassContext
    ) -> IRModule:
        for n_aux_tensor in [2, 3]:
            mod = relax.transform.FuseOpsByPattern(
                [
                    (
                        "decode_take",
                        *decode_take_pattern(n_aux_tensor),
                    )
                ]
            )(mod)
        mod = relax.transform.FuseTIR()(mod)

        for gv, func in mod.functions.items():
            if not isinstance(func, tir.PrimFunc):
                continue
            if "fused_decode" not in gv.name_hint or "take" not in gv.name_hint:
                continue

            downcasted_mod = tir.transform.ForceNarrowIndexToInt32()(
                tvm.IRModule({"main": func})
            )["main"]
            sch = tir.Schedule(downcasted_mod)
            sch.compute_inline("decode")
            mod[gv] = sch.mod["main"]

        return mod
