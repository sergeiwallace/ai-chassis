## Claude Linux profile

This overlay identifies a conventional Linux host using a POSIX shell. Use the distribution package
manager only for system prerequisites; keep project dependencies in the repository environment.
Confirm executable and shell-startup paths before changing them. Use Linux paths such as
`/workspace/project` in examples, and rely on environment variables or local configuration for
service endpoints and credentials.

Machine-local repositories, remote-login endpoints, personal home directories, and private document
inventories are deliberately outside this portable overlay.
