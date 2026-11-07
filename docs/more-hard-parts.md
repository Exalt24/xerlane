# More hard parts

Three entries moved out of the README's "What was hard" list to keep the README short.

- **`init-db` raced itself.** The mutation check runs several workers, each with its own database, and two `init-db` calls at once failed on creating the roles and the schema. It now takes an advisory lock and treats an existing role as done; [test_init_db_run_by_several_processes_at_once_does_not_fail](../tests/test_db.py) runs six at once on a fresh server state.
- **A connection to a closed port hung past two minutes on Windows.** `connect_timeout=10` now bounds it; [test_tenant_is_required_and_the_database_must_be_reachable](../tests/test_cli.py) pins it.
- **Swapping two task codes between versions.** The unique key on (tenant, project, task code) rejected an update that swapped two codes, though the end state was valid. The key is now deferred to commit; [test_swapping_two_task_codes_between_versions_does_not_trip_the_unique_key](../tests/test_load.py) pins it.
