"""Customer-owned foundation recipes and independently approved deployment plans."""

# Recipe used by the existing Core installation flow. Installed 0.2.0/0.3.0 folders
# keep their original file contents; nothing migrates them in place.
RECIPE_VERSION = "0.3.0"
# Recipe used by the infrastructure-only bootstrap milestone. It needs no Core
# release, model or inference artifact; application installation is a later recipe.
INFRASTRUCTURE_RECIPE_VERSION = "0.4.0"
SUPPORTED_RECIPE_VERSIONS = frozenset({"0.2.0", "0.3.0", INFRASTRUCTURE_RECIPE_VERSION})
