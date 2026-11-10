### Fixed

- **A failed `/conexus:setup` says why, and says to read the log before retrying** (nexus-f3yqd.6, F3). When the build, the login-unit registration, the service start, `nx init` or saving the credentials failed, setup reported only "the build exited 2." and advised calling setup again. It now adds the last error line the command printed (or its last line, when none reads as an error), and the next step is to read the log and tell the user the cause, then retry once it is fixed.
