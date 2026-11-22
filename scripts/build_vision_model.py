"""Build app/vision_model/mobilenetv2_features.npz from the ONNX model zoo
MobileNetV2 (Apache-2.0, https://github.com/onnx/models, mobilenetv2-12.onnx).

Keeps only the layers up to the global average pool (1280-d image features),
stores weights as float16 plus a small op list. Run once on a dev machine:

    python scripts/build_vision_model.py mobilenetv2-12.onnx app/vision_model/mobilenetv2_features.npz

Needs ``onnx`` and ``numpy`` (dev only). The app itself needs only numpy.
"""

from __future__ import annotations

import json
import sys

import numpy as np


def main(src: str, dst: str) -> None:
    import onnx
    from onnx import helper, numpy_helper

    model = onnx.load(src)
    inits = {i.name: numpy_helper.to_array(i) for i in model.graph.initializer}
    consts = {}
    ops = []
    arrays = {}
    for node in model.graph.node:
        attrs = {a.name: helper.get_attribute_value(a) for a in node.attribute}
        if node.op_type == "Constant":
            consts[node.output[0]] = numpy_helper.to_array(attrs["value"])
            continue
        if node.op_type == "Conv":
            w = inits[node.input[1]]
            b = inits[node.input[2]] if len(node.input) > 2 else np.zeros(w.shape[0], np.float32)
            key = f"w{len(arrays)}"
            arrays[key + "_w"] = w.astype(np.float16)
            arrays[key + "_b"] = b.astype(np.float32)
            ops.append({"op": "conv", "x": node.input[0], "y": node.output[0], "k": key,
                        "group": int(attrs.get("group", 1)), "stride": int(attrs["strides"][0]),
                        "pad": int(attrs["pads"][0])})
        elif node.op_type == "Clip":
            lo = inits.get(node.input[1], consts.get(node.input[1]))
            hi = inits.get(node.input[2], consts.get(node.input[2]))
            ops.append({"op": "clip", "x": node.input[0], "y": node.output[0], "lo": float(np.asarray(lo).reshape(-1)[0]), "hi": float(np.asarray(hi).reshape(-1)[0])})
        elif node.op_type == "Add":
            ops.append({"op": "add", "a": node.input[0], "b": node.input[1], "y": node.output[0]})
        elif node.op_type == "GlobalAveragePool":
            ops.append({"op": "gap", "x": node.input[0], "y": node.output[0]})
            break
        else:
            raise SystemExit(f"unexpected op {node.op_type}")
    arrays["ops"] = np.frombuffer(json.dumps({"input": model.graph.input[0].name, "ops": ops}).encode(), dtype=np.uint8)
    np.savez_compressed(dst, **arrays)
    print(f"{len(ops)} ops, {sum(a.size for k, a in arrays.items() if k != 'ops')} weights -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
