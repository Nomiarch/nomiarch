package nomiarch.tools_test

import rego.v1
import data.nomiarch.tools

test_included_worker_can_check_config if {
    tools.allow with input as {"identity": "included-worker", "tool": "config.evaluate", "bytes": 100}
}

test_foundation_destroy_is_denied if {
    not tools.allow with input as {"identity": "included-worker", "tool": "foundation.destroy", "bytes": 10}
}

test_unenrolled_identity_is_denied if {
    not tools.allow with input as {"identity": "public-agent", "tool": "config.evaluate", "bytes": 100}
}

test_oversized_input_is_denied if {
    not tools.allow with input as {"identity": "included-worker", "tool": "model.summarize", "bytes": 32769}
}
