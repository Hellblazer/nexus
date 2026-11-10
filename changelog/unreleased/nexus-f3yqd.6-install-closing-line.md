### Fixed

- **`nx self install` names the one command this machine needs next** (nexus-f3yqd.6, F23). Its closing line said "if this upgraded an existing install, run `nx upgrade` ... (a fresh install is set up by `nx init` instead)" and left the choice to you. It now reads the machine: where `nx init` or managed-service onboarding has run (a recorded install mode or service address, or the local service's credentials), it says to run `nx upgrade`; otherwise it says nothing is set up yet and to run `nx init`.
