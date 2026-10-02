# Future idea: connect an already-open terminal session

Requested on 2026-10-02: discover and select an already-open owned terminal or
Claude session, then connect its mailbox without requiring the user to enable
remote control first or return to the machine just to restart it.

This is a post-launch idea. The current bridge's demonstrated path starts a new
session with the plugin enabled. No existing-terminal control feature was built
or claimed as part of the release trial.

A future implementation should investigate supported Claude lifecycle/attach
interfaces and documented desktop tooling. It should visibly identify the exact
owned session, retain its conversation/account/worktree/permissions, respect busy
work, and give the owner a clear connection and disconnect control. Feasibility
is unproven; discovering a process or socket alone does not establish a supported
attachment path. Do not treat this note as permission for arbitrary terminal
input, undocumented injection, or persistent general remote access.

This feature is not required for the demonstrated fresh-session workflow.
