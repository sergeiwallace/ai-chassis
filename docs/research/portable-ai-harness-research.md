# Portable harness research notes

Portable development setup works best when configuration is layered: a generic
base, a tool-specific layer, and a small machine profile. This keeps shared
instructions readable while allowing local choices to remain local.

Use example configuration files and make the copy step explicit. A setup script
should detect its operating system, explain unsupported environments, and avoid
requiring private services. Keep dependency resolution locked when reproducible
automation matters.
