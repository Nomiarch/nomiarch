package nomiarch.tools

import rego.v1

default allow := false

allow if {
    input.identity == "included-worker"
    input.tool in {"config.evaluate", "model.summarize"}
    input.bytes <= 32768
}
