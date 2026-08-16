# Instal & Terapkan

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumen](../../README_ID.md#dokumentasi) · **Instal & Terapkan**

Panduan ini mencakup semua yang Anda butuhkan untuk menjalankan AWLab-ID di project Anda dan menyambungkannya ke agent AI:

1. [Melakukan clone repositori](#1-clone-repositori)
2. [Instalasi python-env dan dependensi server MCP](#2-instalasi-python-venv-dan-dependensi-server-mcp)
3. [Build binary executable](#3-build-binary-executable)
4. [Publikasikan rules & skill ke agent Anda](#4-publikasikan-rules--skill-ke-agent-anda)
5. [Menyambungkan MCP](#5-sambungkan-server-mcp)
6. [Gunakan di Agent Anda](#6-gunakan-di-agent-anda)
7. [Verifikasi pemasangan](#7-verifikasi-pemasangan)
8. [Environment variabel & konfigurasi](#8-environment-variabel--konfigurasi)
9. [Referensi CLI](#9-referensi-cli)
10. [Pemecahan masalah](#10-pemecahan-masalah)

---

## Struktur Project

Tata letak repositori:

```
{root-project}/
├── assets/
│   ├── agents/                  # 1 sub-agent
│   ├── rules/                   # 14 file rules (sumber)
│   ├── skills/                  # 5 sumber skill
│   └── workflows/               # taruh workflow kustom Anda disini (1 default bawaan)
├── dist/
│   └── profiles/                # Output terkompilasi per-agent (dihasilkan oleh compile-rules)
├── src/mcp_server/              # Source code server MCP berbasis python
├── scripts/
│   ├── run.py                   # CLI untuk build & pengembangan
│   └── stop-mcp-servers.ps1     # Helper untuk menghentikan paksa semua server MCP `awlab-*` yang berjalan (Khusus Windows PowerShell)
├── tests/                       # Suite pytest (360 tes)
├── docs/                        # Halaman dokumentasi lainnya
├── CHANGELOG.md
└── pyproject.toml
```

---

## Kebutuhan Instalasi

- **Python 3.10+** (untuk build server MCP dan wajib sudah terinstall sebelumnya)
- **agent-recall** (sebagai backend memori / knowledge-graph)
- **graphify** (melakukan indeks code / code-graph)
- **Model AI** yang mendukung penggunaan tool
- Salah satu dari: **Cline**, **VSCode Copilot**, **Claude Code**, **Hermes Agent**, atau **OpenCode**

**Saran kebutuhan LLM model:** 🟢 Sederhana → lokal 1.5B–3B · 🟡 Menengah → lokal 14B–32B · 🔴 Kompleks → frontier (Claude, GPT)

---

## 1. Clone repositori

```bash
# Clone
git clone https://github.com/awsomplak/awlab-ai-assistant.git

# Masuk ke dalam folder repositori hasil clone
cd awlab-ai-assistant
```

## 2. Instalasi python-venv dan dependensi server MCP

> ⚠️ **Sesuaikan dengan OS Anda.** Perintah aktivasi virtual environment python berbeda antara Windows dan Linux/macOS — melakukan copy paste command yang salah akan berujung gagal.

### Aktivasi python virtual-env

#### Windows (PowerShell)

```powershell
# Buat virtual environment
python -m venv .venv

# Aktifkan python virtual-env di powershell
.venv\Scripts\Activate.ps1

# Jika menggunakan cmd bisa menggunakan command berikut
#
# Aktifkan python virtual-env di cmd
.venv\Scripts\activate.bat
```

#### Linux / macOS

```bash
# Buat virtual environment
python -m venv .venv

# Aktifkan python virtual-env di terminal
source .venv/bin/activate
```

### Instalasi dependensi python

```bash
# Dengan python virtual-venv yang sudah aktif sebelumnya
#
# Instalasi dependensi standar (siap pakai)
pip install -e .

# atau
# Instalasi dependensi untuk development/test (opsional)
pip install -e ".[dev]"
```

---

## 3. Build binary executable

```bash
# Build untuk OS saat ini (menggunakan PyInstaller)
python scripts/run.py build

# Build untuk target tertentu
python scripts/run.py build --target-os=linux
python scripts/run.py build --target-os=all
```

Hasil build ada di `dist/bin/`:

| Binary | Server | Tool yang Tersedia |
|--------|--------|---------------|
| `awlab-ai-assistant{.exe}` | `awlab-ai-assistant` | `action_call` (dispatcher), `action_help` |

Satu executable — dispatcher `action_call` menangani semua operasi (plan, task, memory, graph, context, util, workflow). Binary executable yang sudah dibuild sepenuhnya berdiri sendiri dan tidak memerlukan python lagi atau extensi lainnya dan juga tidak memerlukan file dari project ini.

> **Tips:** untuk pengembangan lokal, Anda bisa menjalankan server langsung dari source (`pip install -e .` + console script `awlab-ai-assistant`) — build executable hanya diperlukan untuk deployment produksi.

---

## 4. Publikasikan rules & skill ke agent Anda

AWLab-ID — **AI-Assisted Development System** menyediakan **14 rules** dan **5 skill** bawaan yang berada di folder `assets/`. Ketika Anda sudah melakukan build dari project ini fungsi `publish` akan  otomatis mempublikasikan profil yang sudah terkompilasi ke direktori agent masing-masing (sesuai target) — fungsi ini cukup dipanggil / dilakukan **sekali saja** per-agent. **SANGAT DISARANKAN** untuk melakukan **BACKUP** jika Anda memiliki pengaturan milik Anda sendiri, cek [target publikasi](#target-publikasi).

```bash
# Publikasikan ke asisten tertentu
python scripts/run.py publish --target=cline     # Cline
python scripts/run.py publish --target=copilot   # VSCode Copilot
python scripts/run.py publish --target=claude    # Claude Code
python scripts/run.py publish --target=hermes    # Hermes Agent
python scripts/run.py publish --target=opencode  # OpenCode
python scripts/run.py publish --target=all       # Semua asisten

# Copot pemasangan
python scripts/run.py publish --uninstall
python scripts/run.py publish --uninstall --target=copilot
```

### Target publikasi

| Target | Rules | Skill |
|--------|-------|-------|
| `cline` | `~/Documents/Cline/Rules/` | `~/.agents/skills/` |
| `copilot` | `~/.copilot/instructions/` | `~/.agents/skills/` (dipakai bersama) |
| `claude` | `~/.claude/CLAUDE.md` | `~/.claude/skills/` |
| `hermes` | — (dikemas sebagai skill) | `~/.hermes/skills/` |
| `opencode` | `~/.config/opencode/AGENTS.md` | `~/.config/opencode/skills/` |

> 💡 Jika Anda melewati panduan §3 - [build binary executable](#3-build-binary-executable), fungsi `publish` akan otomatis melakukan kompilasi profil agent saat folder `dist/` tidak tersedia sebelumnya.
> Selesai — publikasi hanya perlu dilakukan sekali saja. Berikutnya, sambungkan server MCP untuk agent Anda (panduan §5 - [sambungkan server mcp](#5-sambungkan-server-mcp)).

---

## 5. Sambungkan server MCP

MCP server `awlab-ai-assistant` menyediakan **2 tool MCP** — `action_call` dan `action_help` (lihat [Tool MCP yang Tersedia](AVAILABLE_TOOLS.md) untuk detailnya). Menyambungkannya berarti menambahkan **satu entri server MCP** ke konfigurasi agent Anda, arahkan konfigurati MCP-nya ke executable yang Anda build pada panduan [§3](#3-build-binary-executable):

```json
{
  "mcpServers": {
    "awlab-ai-assistant": {
      "type": "stdio",
      "command": "dist/bin/awlab-ai-assistant.exe",
      "args": [],
      "env": {
        "LOG_ENABLED": "true",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

> Dengan konfigurasi MCP server di atas Anda sudah dapat menggunakan tool `action_call`.
> Untuk menambahkan fitur **perekaman pola kebiasaan otomatis Tanpa Token (*zero-LLM*)**
> pada event lifecycle (*tool use*, *prompt*, *session*, *stop*), Anda dapat mengkonfigurasi **Hook** (opsional)
> pada masing-masing agent atau IDE. Penggunaan **Hook** bersifat opsional dan tidak wajib untuk dilakukan.
> Silahkan baca [Registrasi Hook](HOOKS.md) untuk detail selengkapnya.

Gabungkan entri konfigurasi `awlab-ai-assistant` ke server MCP yang sudah ada di agent Anda — jangan mengganti seluruh file konfigurasi — lalu mulai ulang agent/chat. Berikut di bawah ini adalah lokasi konfigurasi masing-masing agent.

### Cline

Tempel bloknya lewat **Cline Settings → MCP Servers → Edit JSON**.

### VSCode Copilot

Tambahkan blok ke `.vscode/mcp.json` (workspace) atau lewat Command Palette → **MCP**.

### Claude Code

Daftarkan server dari terminal:

```bash
claude mcp add awlab-ai-assistant -- dist/bin/awlab-ai-assistant.exe
```

### Hermes Agent

Tambahkan entri di key `mcp_servers:` pada `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  awlab-ai-assistant:
    command: dist/bin/awlab-ai-assistant.exe
    args: []
    env:
      LOG_ENABLED: "true"
      LOG_LEVEL: INFO
```

### OpenCode

Tambahkan entri di key `mcp` ke `~/.config/opencode/opencode.json` (OpenCode memakai objek key `mcp`, bukan `mcpServers`):

```json
{
  "mcp": {
    "awlab-ai-assistant": {
      "type": "local",
      "command": ["dist/bin/awlab-ai-assistant.exe"],
      "enabled": true
    }
  }
}
```

---

## 6. Gunakan di agent Anda

Setelah tersambung, Anda dapat melakukan prompt seperti biasa atau menggunakan slash command:

### Penggunaan skill dalam propt biasa

- *"follow rules"* → memuat registry & plan serta melakukan instruksi kepada agent untuk mengikuti aturan yang ada
- *"create plan"* → membuat rencana implementasi baru dan menulisnya ke dalam `plan.md` serta tugas-tugas yang diperlukan ke dalam `tasks.md`.
- *"start phase 1"* → menjalankan fase pertama dari tugas pada `tasks.md` yang sudah dibuat.

### Penggunaan menggunakan slash command

- `/create plan` → membuat rencana implementasi baru dan menulisnya ke dalam `plan.md` serta tugas-tugas yang diperlukan ke dalam `tasks.md`.
- `/plan-status` → memeriksa status dari plan saat ini akah sedang berjalan, dijeda (paused), atau sudah selesai.
- `/retrospective` → membuat ringkasan serta hasil dari pengerjaan plan yang sedang aktif untuk disimpan ke dalam memori.

---

## 7. Verifikasi pemasangan

1. **Server aktif** — pastikan `util_info` mengembalikan versi + build tag:

   ```
   action_call(action="util_info")
   ```

2. **Project-ID terpasang** — pada respons pertama, agent harus memanggil `project_id` untuk melakukan isolasi memori. Anda bisa memeriksa apakah `.ai/project-id` ada atau sudah dibuat oleh agent. Jika tidak Anda bisa membuatnya sendiri di `.ai/project-id` yang berisikan id dari project saat ini.

3. **MCP aktif** — minta agent Anda menjalankan `action_help` untuk melihat ringkasan perintah masing-masing grup (23 perintah).

---

## 8. Environment variabel & konfigurasi

Pengaturan runtime yang berjalan memiliki prioritas urutan dalam menentukan mana yang harus diambil untuk sumber pengaturan sebagai berikut: **environment variable → `config.json` → pengaturan nilai (value) default**.

- **Development** (ketika dijalankan dari source code): `.env` + `config.json` dibaca dari project root (CWD). Untuk log defaultnya akan ditulis ke `{project_root}/logs`.
- **Production** (binary executable yang sudah dibuild): `.env` + `config.json` dibaca dari lokasi user home `~/.awlab-id/agent-memory/`. Untuk log defaultnya ditulis ke `~/.awlab-id/agent-memory/logs/`.

| Variabel | Default | Deskripsi |
|----------|---------|-------------|
| `AWLAB_ENV` | auto | Penentuan mode saat MCP server berjalan. `production`/`prod` atau `development`/`dev`. Jika kosong, mode akan dideteksi secara otomatis (binary executable yang dibuild dari PyInstaller akan selalu berisi production, selain itu development). |
| `LOG_ENABLED` | `true` | Mengaktifkan/menonaktifkan fitur log (`true`/`1`/`yes`, selain itu maka non-aktif). |
| `LOG_LEVEL` | `info` | Tingkat level log untuk melakukan pencatatan ke file log (contoh: `info`, `debug`, `warning`). |
| `DB_PATH` | (kosong) | Penggantian opsional untuk lokasi database agent-recall. |
| `GRAPH_PARALLEL` | `false` | Ekstraksi code-graph secara paralel dan bersifat opsional (`true`/`1`/`yes`). Lihat di bawah untuk detailnya. |

Pengaturan dapat berupa file `config.json` atau `.env` untuk diterapkan secara global, atau bisa juga diterapkan sebagai environment variable saat mendaftarkan MCP server atau juga dapat diatur langsung dari OS.

### Kapan harus menggunakan `GRAPH_PARALLEL` ?

Proses ekstraksi code-graph saat menjalankan perintah `graph_build` **berjalan berurutan (sekuensial) secara bawaan**, dan ini adalah pilihan terbaik untuk sebagian besar project karena dua alasan utama:

- **Sekuensial Terbukti Lebih Cepat**
  
  Pada skala proyek biasa, proses awal setiap berkas tergolong sangat cepat, sementara proses penggabungan antarberkas jauh lebih dominan dan hanya bisa berjalan pada satu thread. Mengaktifkan proses paralel justru membuat sistem membuang waktu untuk membuat proses baru (terutama di sistem operasi Windows).

- **Bisa Menyebabkan Crash / Berhenti Berjalan**

  Fitur paralel menggunakan `ProcessPoolExecutor`, yang akan berhenti **secara permanen (*hang*)** jika dijalankan dari binary executable (.exe) berbasis *onefile*. Jangan pernah mengaktifkan fitur ini pada build production.

**Ringkasan**

Gunakan `GRAPH_PARALLEL=1` hanya jika:
1. Anda memiliki source code dalam jumlah yang sangat besar.
2. Anda menjalankan MCP server-nya langsung dari source code (menggunakan .venv), bukan dari binary executable yang sudah dibuild.

---

## 9. Referensi CLI

```bash
python scripts/run.py <command> [options]
```

| Perintah | Deskripsi |
|---------|-------------|
| `compile-rules` | Mengompilasi rules dan skill menjadi profil untuk masing-masing agent dan outputnya berada di `dist/profiles/` |
| `build` | Mengompilasi profil agent dan melakukan build binary executable → `dist/` |
| `publish` | Mempublikasikan isi `dist/` ke lokasi masing-masing AI agent |
| `test` | Menjalankan tes python |
| `help` | Menampilkan bantuan terperinci untuk masing-masing perintah |
| `--version` | Menampilkan versi dan build tag |

### compile-rules

```bash
python scripts/run.py compile-rules
```

Mengompilasi `assets/rules/` (14 file rules) dan `assets/skills/` (5 skill) menjadi profil per-agent:

```
dist/profiles/
├── cline/             # File .md individual + skills
├── copilot/           # .instructions.md dengan frontmatter YAML + skills
├── claude/            # Monolit CLAUDE.md + skills
├── hermes/            # Skills sebagai subdirektori SKILL.md
└── .clinerules        # Monolit tingkat-project (tidak dipublikasikan)
```

### build

```bash
# Build penuh (profil + binary executable)
python scripts/run.py build

# Lewati build binary executable
python scripts/run.py build --no-bin

# Lewati kompilasi profil hanya melakukan build binary executable
python scripts/run.py build --no-rules
```

### publish

```bash
# Publikasikan semua target (build dulu jika /dist tidak ada)
python scripts/run.py publish

# Publikasikan ke satu target
python scripts/run.py publish --target=claude

# Lewati build otomatis
python scripts/run.py publish --target=all --skip-build

# Paksa (lewati prompt konfirmasi)
python scripts/run.py publish --force

# Hapus file yang sudah dipublikasi
python scripts/run.py publish --uninstall
```

---

## 10. Pemecahan masalah

| Masalah | Solusi |
|---------|-----|
| `pip install -e .` gagal | Pastikan Python 3.10+ sudah terinstall dan Anda berada di dalam root project. |
| Build gagal / `dist/bin` terkunci | MCP Server yang sedang berjalan mengunci executable. Hentikan dulu server `awlab-*` yang berjalan — lihat `scripts/stop-mcp-servers.ps1` (Windows PowerShell). |
| Agent tidak melihat tool MCP | Daftarkan MCP server (`dist/bin/awlab-ai-assistant{.exe}`) di konfigurasi MCP agent Anda, lalu mulai ulang agent/chat. |
| Kueri graph lambat saat pertama kali | Build pertama adalah ekstraksi penuh dan berjalan di latar belakang — baca ulang setelah selesai (`graph_rebuilding: true` artinya masih membangun). |
| Penggunaan fitur paralel untuk membangun grafik menyebabkan aplikasi .exe tidak merespons | Komponen `ProcessPoolExecutor` mengalami masalah (hang) pada aplikasi hasil kompilasi tipe onefile. Pastikan fitur `GRAPH_PARALLEL` tidak diaktifkan pada versi rilis (production). |
| Data memori yang ditulis hilang tanpa pemberitahuan | Perubahan data akan ditampung di berkas `.ai/memory-bank/pending.jsonl` saat penyimpanan mati (down). Jalankan perintah `mem_replay` setelah sistem pulih untuk menerapkan kembali perubahan tersebut. |

---

## Langkah berikutnya

- Pelajari seluruh fitur yang tersedia: [Tool MCP yang Tersedia](AVAILABLE_TOOLS.md)
- Kembali ke [Halaman utama dokumentasi](../../README_ID.md#dokumentasi)
