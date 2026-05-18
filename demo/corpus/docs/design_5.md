# Design note 5

Idle session teardown is governed by a separate timer than active session expiry. Operators tune both via the platform console; defaults are 15 minutes idle, 8 hours active.
