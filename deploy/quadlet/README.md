# Vibe Flipper με Podman Quadlet

Το Vibe Flipper τρέχει ως systemd service του χρήστη (rootless): ξεκινά στο boot και κάνει restart αν πέσει. Χρειάζεται **Podman 5.2+**, λόγω του αρχείου `.build`. Έλεγχος έκδοσης με `podman --version`.

| Αρχείο | Ρόλος |
|---|---|
| `vibe-flipper.build` | Χτίζει το image `localhost/vibe-flipper` από το clone του repo |
| `vibe-flipper.volume` | Το volume `vibe-flipper-data` με τη βάση SQLite (ίδιο όνομα με το compose) |
| `vibe-flipper.container` | Το service: port 8000, `.env`, healthcheck, restart |

## Εγκατάσταση (rootless)

```bash
# 1. Κώδικας + ρυθμίσεις. Το .build περιμένει το repo στο ~/vibe-flipper
git clone https://github.com/flymperis/vibe-flipper.git ~/vibe-flipper
cp ~/vibe-flipper/.env.example ~/vibe-flipper/.env
#    Ollama στον host: OLLAMA_URL=http://host.containers.internal:11434

# 2. Quadlet units
mkdir -p ~/.config/containers/systemd
cp ~/vibe-flipper/deploy/quadlet/vibe-flipper.* ~/.config/containers/systemd/
systemctl --user daemon-reload

# 3. Εκκίνηση (η πρώτη φορά χτίζει το image, 1–3 λεπτά)
systemctl --user start vibe-flipper.service

# 4. Να τρέχει και χωρίς να είσαι συνδεδεμένος (μία φορά)
loginctl enable-linger "$USER"
```

Το UI ανοίγει στο `http://<server>:8000`. Στην πρώτη εκκίνηση, με άδεια βάση, γίνεται το πλήρες scrape του Insomnia (45–60 λεπτά). Την πρόοδο τη βλέπεις στις Ρυθμίσεις.

Τα units που φτιάχνονται από Quadlet δεν χρειάζονται `systemctl enable`. Το `[Install] WantedBy=default.target` τα ξεκινά αυτόματα στο boot.

Αν έκανες clone σε άλλο φάκελο, άλλαξε το `SetWorkingDirectory=` στο `vibe-flipper.build` και το `EnvironmentFile=` στο `vibe-flipper.container`, και τρέξε ξανά `systemctl --user daemon-reload`.

## Καθημερινή χρήση

```bash
systemctl --user status vibe-flipper.service      # κατάσταση
journalctl --user -u vibe-flipper.service -f      # logs
podman healthcheck run vibe-flipper               # healthcheck χειροκίνητα
```

Για update μετά από `git pull`, ξαναχτίζεις το image και κάνεις restart:

```bash
cd ~/vibe-flipper && git pull
systemctl --user restart vibe-flipper-build.service vibe-flipper.service
```

Αν το update φέρνει νέα προϊόντα στα seed αρχεία:

```bash
podman exec -it vibe-flipper python -m scripts.sync_seed
```

Άλλες εντολές μέσα στο container:

```bash
podman exec -it vibe-flipper python -m scripts.scrape_now --full --source insomnia   # πλήρες scrape
podman exec -it vibe-flipper python -m scripts.eval_matching                         # σύγκριση matching
```

## Ollama σε container

Αν το Ollama τρέχει κι αυτό ως container σε δικό του podman network, βγάλε το σχόλιο από το `Network=` στο `vibe-flipper.container`, βάλε το δικό σου `.network` unit, και ρύθμισε `OLLAMA_URL=http://<όνομα-container-ollama>:11434`.

## Rootful (προαιρετικά)

Βάλε τα αρχεία στο `/etc/containers/systemd/` και χρησιμοποίησε `systemctl` χωρίς `--user`. Το `%h` τότε σημαίνει `/root`, οπότε όρισε απόλυτες διαδρομές στα `SetWorkingDirectory=` και `EnvironmentFile=`.
