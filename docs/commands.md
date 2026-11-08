# Commands and exit codes

| Command | What it does | Exit code |
|---|---|---|
| `init-db` | Creates the two roles, the schema and the tables. Safe to run twice. | 0 |
| `load FILE --tenant T` | Parse, validate, load. | 0 clean, 1 loaded with quarantined rows, 2 file rejected or a project failed |
| `validate FILE [--tenant T]` | Check the file; with a tenant also the stored data of its projects. | 0 clean, 1 errors found |
| `reconcile FILE --tenant T` | Prove the stored data equals the file. | 0 matches, 1 differences |
| `diff FILE --tenant T` | What loading the file would change, without writing. | 0 nothing, 1 changes |
| `load-cost FILE --kind K --tenant T` | Load a cost or commitment CSV (`K` is `cost` or `commitment`). | 0 clean, 1 loaded with set aside rows, 2 rejected or failed |
| `reconcile-cost FILE --kind K --tenant T` | Prove the stored rows equal the file. | 0 matches, 1 differences |
| `report --tenant T --proj-id N` | Earned value for one project (`--as-of`, `--format` text, csv or html, `--out`). | 0 |
| `add-user --tenant T --email E --role R` | Create a web app user, `R` is viewer or admin (password from `XERLANE_NEW_PASSWORD` or a prompt). | 0, 2 on a refused user |

The loader refuses to run as a superuser or any role that bypasses row level security, because that role would see every tenant.
