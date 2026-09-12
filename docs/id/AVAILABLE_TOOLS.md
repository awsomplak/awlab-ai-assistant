# 🧰 Tool MCP yang Tersedia

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumentasi](../../README_ID.md#dokumentasi) · **Tool MCP yang Tersedia**

> MCP server memiliki **2 tool**: `action_call` (dispatcher) dan `action_help` (bantuan), yang menangani **26 action**.

**Di halaman ini:**

- [Arsitektur server](#arsitektur-server)
- [Tool yang tersedia](#tool-MCP-yang-tersedia)
- [Daftar Action](#daftar-action)
- [Pengolahan User Pattern](#pengolahan-user-pattern)
- [Cache offline (`pending.jsonl`)](#cache-offline-pendingjsonl)
- [Project Family](#project-family)

---

## 🏛️ Arsitektur Server

| Executable               | Nama Server          | Tool yang Tersedia           |
| ------------------------ | -------------------- | ---------------------------- |
| `awlab-ai-assistant` | `AWLab-AI-Assistant` | `action_call`, `action_help` |

> Deployment produksi adalah **pasangan bridge + worker**: `awlab-ai-assistant` adalah
> *bridge* — proxy stdio tipis (entrypoint tunggal yang dipakai semua konfigurasi IDE/hook) —
> yang menjalankan `awlab-ai-worker`, server MCP berat tempat `REGISTRY` menangani semua
> action. Bridge menjaga pipa JSON-RPC IDE tetap hidup, sehingga `publish --target=binary`
> dapat mengganti (*hot-swap*) worker tanpa membuat IDE mengalami `context canceled`.

### 🛡️ Proses Lifecycle — tidak ada worker yang menjadi orphan

Pasangan bridge + worker tidak pernah meninggalkan proses *orphan* (di semua OS):

- **Worker orphan watchdog** — bridge mengirim PID-nya ke worker (`AWLAB_BRIDGE_PID`);
  *daemon watchdog* berbasis stdlib/ctypes memantau parent, dan worker akan menghentikan diri
  — membatalkan background rebuild yang sedang berjalan lebih dulu — begitu parent (bridge)
  mati.
- **Bridge parent watchdog** — `awlab-ai-assistant` adalah PyInstaller ONEFILE, jadi proses
  yang dikelola agent/IDE adalah *bootloader parent*-nya. Bridge asli memantau parent tersebut dan,
  saat mati, menutup pohon worker lalu keluar.
- **Tree teardown di semua jalur keluar** — di Windows, worker dibungkus Job Object dengan
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (OS menutup worker + turunannya bahkan saat di-*force*
  -kill) plus `taskkill /T`; di POSIX, `killpg` (SIGTERM → SIGKILL) dikirim ke *process group*
  worker. Saat stdin-EOF, worker diberi waktu 5 detik, lalu di-*force*-kill.
- **Background rebuild yang bisa dibatalkan** — loop chunk-drain berhenti di antara chunk saat
  worker sedang shutdown (signal / orphan watchdog / stdio EOF).
- **Build frozen tidak pernah membuat pool children** — `GRAPH_PARALLEL` diabaikan di dalam
  executable (multiprocessing spawn akan menge-*re-exec* `awlab-ai-worker` sebagai pool
  children).
- **Hook daemon membersihkan diri** — daemon background berhenti sendiri setelah 5 menit tanpa
  traffic hook dan tidak pernah meninggalkan `daemon.port` yang basi.

---

## 🛠️ Tool yang Tersedia

### `action_call(action, params=None)`

Tool berikut berfungsi mengirim sebuah action ke MCP Server. Server MCP akan menjalankan preconditions/pipeline secara otomatis. Setiap respon akan menyertakan `executed`/`skipped`. Contoh penggunaan:

```
action_call(action="task_read", params={"plan_uuid": "MCPtool1", "format": "structured"})
```

### `action_help(action=None)`

Tool berikut berfungsi menampilkan informasi bantuan penggunaan untuk setiap action (params, default, contoh, preconditions, pipeline) atau ringkasan per grup jika dipanggil tanpa argumen.

---

## 📋 Daftar Action

### 🔖 context

<details><summary><b>View context actions</b></summary>

| Action       | Ringkasan                                                                                                                                                                                                            |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ctx_info`   | Membaca konteks project: snapshot, memory-bank, scan, saran, atau konteks orkestrasi.                                                                                                                                |
| `project_id` | Memeriksa Project ID; otomatis membuatnya jika belum ada (idempotent). Panggil ini pada respons pertama, sebelum operasi `mem_*`/plan, agar isolasi memori berjalan optimal dan terisolasi tidak masuk ke DB global. |

</details>

### 🔖 memory

<details><summary><b>View memory actions</b></summary>

| Action              | Ringkasan                                                                                                                                                                                                                                          |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mem_dedupe`        | Menggabungkan entitas memori yang senama (simpan yang berisi data, arsipkan duplikat).                                                                                                                                                             |
| `mem_list_entities` | Mendaftar semua entitas memori (nama/tipe/jumlah obs) untuk audit.                                                                                                                                                                                 |
| `mem_observe`       | Mencatat pola pengguna ke observation store (`.ai/memory-bank/observations.jsonl`) — input untuk pipeline lanjutan khusus user pattern.                                                                                                            |
| `mem_read`          | Membaca detail node atau lingkungan graph.                                                                                                                                                                                                         |
| `mem_remove`        | Mengarsipkan entitas atau menghapus observasi/relasi (type-safe — menolak nama yang ambigu).                                                                                                                                                       |
| `mem_replay`        | Mengimpor cache offline (`.ai/memory-bank/pending.jsonl`) — menjalankan queue saat store/MCP tidak terjangkau sebelumnya. Entri yang gagal tetap disimpan untuk dicoba ulang. `dry_run` untuk melakukan pratinjau.                                 |
| `mem_search`        | Pencarian hybrid BM25+dense di memori (opsional berdasarkan tipe entitas). store=patterns + scope/context untuk pola pengguna yang cakupannya sama ke dalam susunan yang terorganisir, store=family\_<slug> untuk memori project yang berkorelasi. |
| `mem_write`         | Membuat/menandai entitas, menambah observasi, atau menghubungkan entitas.                                                                                                                                                                          |

#### Ekstraksi Memori LLM (Opsional)

Untuk `mem_observe` dan `mem_write`, Anda dapat mengirimkan teks tidak terstruktur melalui parameter `raw_text`. Jika dikonfigurasi dengan `LLM_API_KEY` (lihat `.env.example`), server akan secara cerdas mengekstrak observasi dan entitas terstruktur dari teks mentah tersebut menggunakan LLM sebelum menyisipkannya ke dalam SQLite Memory Bank. Jika tidak dikonfigurasi, sistem akan beralih ke logika deterministik standar yang tetap aman.

</details>

### 🔖 plan

<details><summary><b>View plan actions</b></summary>

| Action        | Ringkasan                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `plan_status` | Membaca status plan/registry: plan aktif, task berikutnya, status dari task (selesai, pending, belum dieksekusi/dijalankan), fase dari task (fase berisi beberapa task).                                                                                                                                                                                                                               |
| `plan_update` | Mengubah plan/registry: dipindahkan ke plan aktif, menandai fase selesai, menyelesaikan task yang ditunda.                                                                                                                                                                                                                                                                                             |
| `plan_doc`    | Membaca / membuat / memperbarui / menghapus `plan.md` atau `notes.md` suatu plan secara langsung.                                                                                                                                                                                                                                                                                                      |
| `reg_update`  | Membaca / membuat / memperbarui `registry.md`, berikut detail singkatnya: <br> `create`: plan UUID artifact dibuat oleh MCP → plan baru ditandai sebagai plan yang Aktif ⏹️. <br> `update`: memperbarui status plan menjadi active\|paused\|complete → diletakkan ke tabel yang sesuai dengan status. <br> `delete`: Untuk menghapus plan dan butuh persetujuan ketat pengguna lewat `confirmed=true`. |

</details>

### 🔖 graph

<details><summary><b>View graph actions</b></summary>

| Action          | Ringkasan                                                                                                                                                                                                                                                                                                                                       |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `graph_build`   | Membangun/memperbarui Code Knowledge Graph ke `.ai/codegraph/`. Sistem ini menggunakan **LanceDB** untuk vektor embedding (struktur AST + pencarian semantik). Untuk multi-project (`family=<slug>`) MCP akan membangun graph family GABUNGAN. Proses build inkremental berjalan sangat cepat karena hanya mengekstrak ulang file yang berubah. |
| `graph_status`  | Melaporkan status Code Graph (tersedia? kedaluwarsa? terdapat perubahan file?).                                                                                                                                                                                                                                                                 |
| `graph_query`   | Mencari di dalam Code Graph (label / file source / tipe). Otomatis diperbarui dulu.                                                                                                                                                                                                                                                             |
| `graph_path`    | Jalur terpendek antara dua node graph. Otomatis diperbarui dulu.                                                                                                                                                                                                                                                                                |
| `graph_explain` | Menjelaskan node graph (detail + korelasi). Otomatis diperbarui dulu.                                                                                                                                                                                                                                                                           |

#### Cakupan (scope) yang diindeks

- Graph menggunakan **LanceDB** untuk mengindeks label tingkat file, fungsi, class, dan komponen melalui vektor embedding.
  Variabel lokal/computed/ref/prop **bukan** bagian dari node.
- Saat `graph_query` **tidak menemukan node** untuk suatu istilah, sistem beralih ke fungsi alternatif **pemindaian sumber kata utuh**
  dan mengembalikan hasil kecocokan tingkat file dengan `type: "identifier"` dan `mode: "identifier"` — jadi kueri untuk
  sebuah variabel (mis. `brakeBaselineDays`) tidak pernah menemui jalan buntu.
- **Identitas node terpadu**: semua action graph menerima node `id` (seperti yang dikembalikan
  `graph_query`) maupun label. `graph_path`/`graph_explain` melakukan resolusi dengan urutan id → label → jalur file source →
  nama fungsi → substring, sehingga navigasi lintas-file berfungsi dengan label, id, atau jalur file.
- `graph_path` mencari jalur **tingkat-simbol** terlebih dulu dan bila tidak ditemukan, maka beralih ke jalur **tingkat-modul**
  melalui edge `imports_from`/`imports` (`mode: "module"`), dan jika tetap tidak ditemukan, ia melaporkan
  diagnostik "no path" yang lengkap dengan kedua file source.
- **Import path-alias Vite/JS ikut terindeks** (`@/stores/auth`, `@pages/...`, `~/components/...`).
  Secara default graphify hanya mendukung import relatif + `paths` tsconfig/jsconfig. AWLab-AI-Assistant **AI-Assisted Development System** menambahkan langkah pasca-build yang membaca `resolve.alias` dari `vite.config.*` / `nuxt.config.*` (dalam bentuk objek atau array, termasuk penggantian `fileURLToPath(new URL(...))`) dan menambahkan edge
  `imports_from`/`imports` yang hilang — sehingga SFC `.vue` dan file yang meng-import `@/` tetap
  terhubung di `graph_path` walau tidak ada `tsconfig.json`. Edge hasil-alias membawa
  `alias_resolved: true`; langkah ini idempotent dan juga memperbaiki sendiri graph lama (tanpa build ulang penuh).
- **Pengecualian (exclusion)** — mengikuti sintaks gitignore dan bersifat kumulatif:
  graph selalu mematuhi `_NOISE_DIRS` (`.git`, `.venv`, `node_modules`, `dist`, `build`, `vendor`, …) dan file kunci dependensi (lock file). Selain itu, graph juga mengikuti `.gitignore` project, serta **`.graphignore`** yang bersifat lokal — Anda dapat mengecualikan file/direktori dari code graph saja (misalnya kode hasil generate atau salinan vendor) tanpa memengaruhi git. `.gitignore` dan `.graphignore` diparsing menjadi SATU set aturan aditif dengan SEMANTIK GLOB yang IDENTIK untuk file DAN direktori (`dist-*/`, `**/cache/` memangkas seluruh subtree; nama/path persis mengecualikan file atau direktori). Pola `*`/`**` (ignore-all, yang butuh re-inclusion `!`) dilewati, sehingga `.gitignore` ala Laravel tidak pernah secara tidak sengaja mengecualikan semua file. Perubahan pada `.graphignore` akan memicu pembangunan ulang.

#### Pembuatan bertahap (chunking) untuk project besar

Pada project yang besar, pembangunan graph secara penuh dapat menyebabkan lonjakan penggunaan RAM/CPU. Agar prosesnya tetap lancar, `graph_build` memproses file secara **bertahap dalam potongan (chunk) yang dibatasi** — mengikuti pola antrean (queue):

| Parameter / Env                   | Default        | Keterangan                                                                                                                                                                                                                                                  |
| --------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chunk_size` / `GRAPH_CHUNK_SIZE` | `200`          | Jumlah maksimal file yang diproses dalam satu kali build. Manifest hanya diperbarui untuk file yang sudah diproses, dan hasilnya mengembalikan `processed_files` / `remaining_files` / `chunked`.                                                           |
| `max_files` / `GRAPH_MAX_FILES`   | (tidak diatur) | Membatasi jumlah file pada build pertama (chunk awal).                                                                                                                                                                                                      |
| `background`                      | `true`         | Pemicu tanpa menunggu: permintaan langsung dikembalikan dan worker latar belakang memproses chunk hingga `remaining_files == 0`; gunakan `false` untuk memproses satu chunk secara sinkronus (tidak pernah diblokir oleh build ulang yang sedang berjalan). |
| `force`                           | `false`        | Melewati guard in-flight dan memulai build baru walau ada rebuilding flag/worker yang basi (escape hatch untuk keadaan stuck).                                                                                                                              |

- **Penggunaan sumber daya yang stabil** — setiap build hanya memproses maksimal `chunk_size` file, sehingga puncak RAM/CPU tetap terkendali, bukan melonjak sekaligus; ini ideal untuk project yang sangat besar.
- **Progres mudah dipantau** — `graph_build` dan `graph_status` melaporkan `processed_files` dan `remaining_files`; nilai `remaining_files == 0` berarti graph sudah lengkap.
- **Tidak ada pembacaan graph parsial** — selama build belum selesai, `graph_query`, `graph_path`, dan `graph_explain` mengembalikan `mode: "pending"` tanpa node, bukan menyajikan hasil graph yang parsial atau kedaluwarsa. Coba lagi setelah `graph_status` melaporkan `fresh: true`.
- **Selesai secara otomatis** — setiap pembacaan graph (`graph_query`/`graph_path`/`graph_explain`) memicu `graph_fresh` → `ensure_fresh(background=True)`, yang menjalankan proses chunk di latar belakang, sehingga pembacaan ikut mempercepat penyelesaian build secara bertahap.
- `node_limit` (default `20000`, via `GRAPHIFY_VIZ_NODE_LIMIT`) membatasi `graph.html` interaktif; graph yang melebihi batas ditampilkan dalam bentuk agregasi per kelompok (bukan gagal); nilai `0` menonaktifkan HTML sepenuhnya.

**Akurasi `graph_status`** — `processed_files` bersifat kumulatif (`total_files − remaining_files`), plus `processed_total`, `processed_this_chunk` (per-run), `remaining_files`, `chunked`, dan visibilitas pengecualian `scanned_files` / `excluded_files` / `supported_files`. `rebuilding` mencerminkan worker latar belakang yang aktif (persisted `rebuilding: true` yang basi tanpa worker aktif otomatis dibersihkan saat dibaca); `background_error` menampilkan kegagalan worker terakhir dan bertahan setelah server restart. `background: false` selalu memproses satu chunk secara sinkronus (tidak pernah diblokir — perbaikan Bug 3); `force: true` melewati guard sepenuhnya.

**HTML graph skala besar (`graph.html` / `family.html`)** — setiap visualisasi yang dirender menyertakan lapisan sisi-klien mandiri agar project dengan ribuan node tetap bisa digunakan:

- **Bilah filter** — filter node berdasarkan path file (`src/components`), berdasarkan derajat minimum (merapikan tampilan), atau aktifkan **Focus 2-hop** untuk menampilkan hanya lingkungan node yang dipilih. Tepi (edges) dipotong hanya ke node yang terlihat. Tombol header **Filters** bisa menciutkan/memperluas bilah agar ruang sidebar lebih lega.
- **Pengaman physics** — di atas ±2000 node yang terlihat, layout forceAtlas2 dinonaktifkan (agar browser tidak membeku); persempit tampilan dengan filter, lalu tekan **Stabilize**.
- **Panel yang bisa diubah ukurannya** — seret pemisah antara **Node Info** dan **Communities** untuk memberi ruang lebih ke salah satu panel (klik dua kali untuk mengatur ulang); kotak Node Info menggulir ke dalam bila node punya daftar tetangga panjang, sehingga tidak menimpa atau mendorong legenda Communities.
- **Drill-down komunitas** — saat graph melebihi `node_limit` (tampilan agregasi per komunitas), saat klik node komunitas membuka daftar anggota yang bisa dicari, lalu **Load members into graph** membangun ulang tampilan dari node + tepi anggota komunitas tersebut; **Reset** kembali ke tampilan ringkasan. Turunkan `node_limit` (mis. `1500`) untuk mendapatkan ringkasan agregasi + drill-down pada project besar.

#### Alur pembentukan Code Graph

Setiap pembacaan Code Graph (`graph_query`, `graph_path`, `graph_explain`) mengembalikan field metadata ini agar agent selalu tahu apakah datanya masih baru dan apakah ada build ulang yang sedang berjalan:

| Field              | Tipe   | Makna                                                                                        |
| ------------------ | ------ | -------------------------------------------------------------------------------------------- |
| `graph_fresh`      | `bool` | Apakah graph yang disajikan terkini saat dibaca (source tidak berubah sejak build terakhir). |
| `graph_exists`     | `bool` | Apakah graph sudah ada (`false` pada pembacaan pertama).                                     |
| `graph_rebuilding` | `bool` | `true` saat build ulang resource yang besar, proses akan berjalan di latar belakang.         |
| `graph_built_at`   | `str`  | Timestamp ISO dari build sukses terakhir.                                                    |

**Pola pembaruan data:**

- Graph dengan **sedikit perubahan file** → dibuild ulang secara **sinkronus** sebelum operasi pembacaan (hasilnya akurat).
- Graph dengan **banyak perubahan file (≥ 20) atau saat build pertama** → dibuild ulang di **latar belakang**, karena tidak dapat memberikan hasil secara langsung ketika agent memanggil fungsi ini maka akan ditampilkan data graph sebelumnya jika ada. Jika `graph_rebuilding: true`, agent akan menunggu sejenak lalu membaca kembali graph yang sudah di build ulang (pembacaan berikutnya akan menampilkan data terbaru).
- `graph_build` (eksplisit) yang dipanggil saat build ulang yang sedang berlangsung di latar belakang akan **digabungkan (coalesced)** — operasi ini mengembalikan status `rebuilding: true` alih-alih memulai proses pembangunan ulang yang sama.

</details>

### 🔖 task

<details><summary><b>View task actions</b></summary>

| Action        | Ringkasan                                                                |
| ------------- | ------------------------------------------------------------------------ |
| `task_read`   | Membaca tasks.md dari plan dalam bentuk JSON terstruktur/mentah/minimal. |
| `task_update` | Membuat atau memperbarui tasks.md / task.                                |

</details>

### 🔖 util

<details><summary><b>View util actions</b></summary>

| Action      | Ringkasan                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------- |
| `util_info` | Menampilkan informasi versi server MCP / metadata project (atau pembuatan mermaid diagram). |

</details>

### 🔖 workflow

<details><summary><b>View workflow actions</b></summary>

| Action | Ringkasan                                                                                                                             |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `wf`   | Mendaftar atau menjalankan sebuah workflow yang berada di `~/.awlab-id/agent-memory/work-flows` sesuai dengan nama file workflow nya. |

---

</details>

## 🧁 Pengolahan User Pattern

Server MCP mengolah kebiasaan pengguna yang berulang menjadi opsi yang bisa dipakai ulang:

1. **Observe** — `mem_observe` (yang dijalankan oleh agent saat live chat) atau dari `awlab-ai-assistant hook --agent <agent> --event <event>` (event lifecycle agent/IDE milik agent) menambahkan action ke `.ai/memory-bank/observations.jsonl` untuk melakukan penghapusan duplikat data berdasarkan _fingerprint_.
2. **Bake / Proses Pengolahan** — setiap `action_call` menjalankan `bake_tick` dengan alur kerja `baca → key → hitung → consistency → confidence`. Pattern / pola yang sudah diolah kemudian ditulis ke `.ai/memory-bank/baked.json` hanya jika berubah. Confidence = `frequency(min(1,count/5)) × consistency × source_weight` (`explicit`/`corrected` 0.9, `behavioral` 0.6, `inferred` 0.4). Sebuah pattern / pola butuh `count ≥ 2 ∧ consistency ≥ 0.5 ∧ confidence ≥ 0.6`.
3. **Deliver (tell-once)** — `ctx_info mode="context"` / `mem_search store="patterns"` mengembalikan atau menghasilkan `pattern_candidates` / `baked_patterns` (scoped ke stack). Penanda dalam sistem pengiriman mencatat pola kebiasaan yang telah disampaikan, sehingga pola tersebut TIDAK AKAN PERNAH disampaikan ulang sampai ada pola baru yang mematangkan pola tersebut (_baked_).

**Tiga tahap, satu penyimpanan** — berikut daftar tahapnya:

1. inline (per `action_call`) yang dilakukan oleh agent ketika user melakukan prompt via live chat dengan agent,
2. async (bake-scheduler yang berjalan di latar belakang dan memproses ulang pola / user pattern di workspace yang aktif)
3. sub-agent (`awlab-baker`, yang berjalan jika ada pola baru)
   ketiganya menggunakan `observations.jsonl` dan `baked.json` yang sama, sehingga pola yang dihasilkan identik apa pun tahapnya.

**Mode hook (opsional)** — `awlab-ai-assistant hook --agent <agent> --event <event>` menangkap observasi dari event lifecycle agent/IDE (prompt pengguna, penggunaan tool, mulai/selesai-nya sesi, proses dari sub-agent). Registrasi hook wajib dilakukan per-AI agent. Executable (MCP yang sudah dibuild contoh dalam bentuk .exe pada windows) menentukan project per event (lihat [`INSTALL.md`](INSTALL.md)).

## 💾 Cache offline (`pending.jsonl`)

> [!NOTE]
> Saat gagal menyimpan atau server MCP tidak bisa dijangkau, data akan disimpan dalam bentuk **> **antrian (queue)**** — ke `.ai/memory-bank/pending.jsonl`:

- **Sisi server (otomatis):** `mem_write`/`mem_remove` saat store mati, atau `task_update` saat DB-sync mati → operasi diantrekan otomatis.
- **Sisi agent (MCP mati):** ketika agent melakukan `mem_write` / `mem_remove` / `task_update` data akan ditulis ke dalam file JSONL memakai tool file Anda sendiri atau dari IDE atau menulisnya secara manual jika agent memiliki kapabilitas untuk melakukan edit pada perangkat Anda, namun jika dilakukan secara manual tidak menutup kemungkinan data tersebut tidak disimpan atau tidak ditulis oleh agent (sesuai aturan / rules [`14-MCP-offline-cache`](../../assets/rules/14-MCP-offline-cache.md)).
> - **Proses Impor ulang:** `mem_replay` mengimpor antrean (queue) data dari cache yang tersimpan offline dari file JSONL (bila ada file atau datanya) — entri yang sukses dijalankan akan dihapus, dan >   yang gagal akan disimpan kembali untuk dicoba ulang. Fitur `dry_run` akan melakukan pratinjau terlebih dahulu sebelum benar-benar dijalankan atau dieksekusi.

## 👨‍👩‍👧‍👦 Project Family

Project gabungan yang berkorelasi meski di lokasi (path atau drive) yang berbeda dan berbagi Code Graph gabungan serta penyimpanan memori khusus bernama `family_<slug>`. Untuk petunjuk penyiapan lengkap, lihat [Konfigurasi Project Families](PROJECT_FAMILIES.md). File `~/.awlab-id/agent-memory/project-families.json` mendaftarkan setiap project kedalam grup dengan bentuk seperti berikut:

```json
{
  "group-atau-family-key": {
    "name": "Nama Group atau Family",
    "members": [
      {
        "path": "/lokasi/path/project/satu",
        "project_id": "id_project_satu"
      },
      {
        "path": "/lokasi/path/project/dua",
        "project_id": "id_project_dua"
      }
    ]
  }
}
```

Jika `Project ID` yang terdaftar pada file `project-families.json` berbeda dengan `.ai/Project ID` dari project, maka akan lebih diutamakan menggunakan `Project ID` dari project tersebut daripada `Project ID` yang **dideklarasikan** manual di dalam file `project-families.json` (akan diperbarui otomatis saat build graph family berjalan) karena `project-families` berbasis path dari project sebagai acuan utama. Penambahan project baru ke dalam `project-families.json` akan otomatis diinisialisasi (**seeded**), dan perintah `graph_build` dengan parameter `family=<slug>` akan menghasilkan **Code Graph** gabungan yang memuat _node_ dengan prefix `<project_id>::`. Setiap project anggota memiliki penanda `.ai/family-id` (kunci family **utama**, meniru `.ai/Project ID`), dan `ctx_info`/`family_info` melaporkannya beserta semua family yang dimiliki project tersebut. `family_info` (hanya-baca) mendaftar dan me-resolve family; `family_config` memungkinkan agent membuat/mengubah/menghapus family beserta anggotanya — pengguna hanya memantau filenya.
