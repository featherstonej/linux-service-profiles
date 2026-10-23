# linux-service-profiles

Create profiles that start and stop systemd services when selected.

```
./service_profiles.py create gaming --stop cups.service mariadb.service
./service_profiles.py create coding --start mariadb.service postgresql.service
./service_profiles.py list
./service_profiles.py apply gaming --dry-run   # preview
sudo ./service_profiles.py apply coding        # use --user for user services
```

Profiles are stored in `~/.config/linux-service-profiles/profiles.json`
(override with `--config`). Run tests with `python3 -m unittest discover tests`.
