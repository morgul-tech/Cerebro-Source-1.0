# BK07 Liv local session host candidate

This host prepares a Liv-owned `local:` session reference while the normal
BK07 profile remains off. It performs no OAuth call, NATS connection, PM/X9
operation or Context transition. One running process owns one reference;
restart creates a new one. The reference is written atomically with a
heartbeat to `session-host-state/session.json`. `PREPARED_UNBOUND` is only a
local host fact. It is never evidence of an authenticated or current Context
session.

Before using a reference, call `read_current_local_session` with the exact
profile and state directory. It requires the OFF profile, a heartbeat no older
than 15 seconds, the original live process creation time, and an actively
held host lock. A crashed process can leave a `PREPARED_UNBOUND` file behind;
that file fails this readback because its process or lock is gone. A restarted
host receives a different reference, so the provider must bind it afresh.

`Install-BK07LocalSessionHost.ps1` copies the reviewed script by exact hash
and registers one Windows startup task under SYSTEM. It refuses to replace an
existing task or different installed script. The accepted source SHA256 is
hard-coded in the installer; a different caller-supplied script fails before
any installation step. `-StartNow` is deliberate and
optional; the default only registers the task. The task keeps the profile
off and exits if it becomes enabled, so the normal listener must later be
started by the separately qualified host connector. The installer does not
touch credentials or the historical transport-only binding.

The Context operator may use the live local reference only after a fresh
`read_current_local_session` result and verifying the selected host and source
hash. The reviewed
provider-owned Context bootstrap then persists and rereads that exact
`LOCAL_CEREBRO_RUNTIME` binding while `enabled=false`. A stopped process or
new local reference requires a fresh provider binding. The PM/X9 and
read-only resolver qualification gates remain separate.
