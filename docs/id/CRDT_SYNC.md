# Dukungan Memori Cloud (CRDT Sync)

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumen](../../README_ID.md#dokumentasi) · **Dukungan Memori Cloud (CRDT Sync)**

Secara bawaan, Memory Bank milik AWLab-AI-Assistant beroperasi sepenuhnya secara luring (offline) menggunakan SQLite lokal (`agent-recall`). Hal ini menjamin isolasi proyek yang sempurna, privasi data yang ketat, dan latensi nol (zero latency). Namun, bagi para pengguna mahir yang ingin menyinkronkan memori AI mereka di beberapa perangkat atau melalui _cloud_, kami menyediakan dukungan eksperimental untuk **CRDT (Conflict-Free Replicated Data Types)** melalui ekstensi `cr-sqlite`.

---

## Cara kerjanya

Ketika server MCP AWLab-AI-Assistant tersambung ke dalam database memori SQLite, server secara aktif akan mencari keberadaan _compiled extension_ `cr-sqlite`. Jika ekstensi tersebut ditemukan, maka akan langsung disuntikkan ke dalam koneksi SQLite tersebut.

Hal ini memberikan berkas `memory.db` lokal Anda kemampuan CRDT, yang berarti database tersebut dapat direplikasi dan digabungkan (merge) dengan database lain di seluruh jaringan tanpa menimbulkan konflik sinkronisasi.

> [!WARNING]
> AWLab-AI-Assistant menangani pemuatan ekstensi dan membuat database tersebut sadar akan CRDT. Aplikasi ini **tidak** menyertakan _daemon_ sinkronisasi jaringan di latar belakang. Anda bertanggung jawab untuk menjalankan dan mengelola agen penyinkron secara mandiri (misalnya menggunakan _sync server_ standar seperti `vlcn.io` atau skrip replikasi khusus) untuk benar-benar mengirim dan menyimpan data ke _cloud_.

---

## Panduan Instalasi

Untuk mengaktifkan dukungan CRDT, Anda perlu menempatkan pustaka bersama (shared library) `cr-sqlite` yang telah dikompilasi ke dalam direktori konfigurasi yang tepat.

### 1. Unduh Ekstensi

Unduh ekstensi `crsqlite` yang sudah dikompilasi sesuai dengan sistem operasi Anda dari [halaman rilis resmi vlcn.io](https://github.com/vlcn-io/cr-sqlite/releases).

- **Windows:** `crsqlite.dll`
- **macOS:** `crsqlite.dylib`
- **Linux:** `crsqlite.so`

### 2. Simpan di Direktori Konfigurasi

Buat sebuah folder bernama `extensions` di dalam direktori konfigurasi utama (`~/.awlab-id/agent-memory/`) dan pindahkan file yang telah diunduh ke dalamnya.

```bash
# Contoh untuk Linux/macOS
mkdir -p ~/.awlab-id/agent-memory/extensions/
mv ~/Downloads/crsqlite.so ~/.awlab-id/agent-memory/extensions/crsqlite.so
```

> [!NOTE]
> Pastikan nama filenya tepat: `crsqlite.dll`, `crsqlite.dylib`, atau `crsqlite.so` tergantung pada OS Anda. Sistem akan mencari nama file yang spesifik ini.

### 3. Jalankan Ulang (Restart) Server

Setelah file ditempatkan dengan benar, Anda cukup menjalankan ulang AI agent Anda. Server MCP akan mendeteksi ekstensi secara otomatis, menambal (patch) koneksi SQLite, dan mengaktifkan `cr-sqlite`. Anda akan melihat keterangan pemuatan ekstensi ini pada catatan log debug jika Anda mengaktifkan pengaturan logging (`LOG_LEVEL=DEBUG`).

---

## Menyinkronkan ke Cloud

Setelah ekstensi berhasil dimuat, berkas `memory.db` Anda siap untuk disinkronkan.

Anda dapat menggunakan server sinkronisasi resmi `vlcn.io` atau metode sinkronisasi SQLite apa pun yang kompatibel dengan CRDT untuk mereplikasi direktori `~/.awlab-id/agent-memory/` (atau direktori `.ai/memory-bank/` yang diisolasi per proyek) ke server _cloud_ terpusat. Karena data tersebut sekarang sudah mendukung CRDT, suntingan memori yang dilakukan pada laptop Anda akan menyatu (merge) dengan mulus dengan perubahan yang Anda buat di komputer desktop!
