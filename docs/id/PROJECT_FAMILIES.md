# 👨‍👩‍👧‍👦 Konfigurasi Project Families

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumentasi](../../README_ID.md#dokumentasi) · **Project Families**

> **Singkatnya (TL;DR):** Untuk berbagi memori dan *code graph* di antara project yang saling berkaitan (contoh: backend + frontend), buat file `~/.awlab-id/agent-memory/project-families.json` yang mengelompokkan *path* project-project tersebut. AI akan mendeteksinya secara otomatis.

**Project Family** memungkinkan Anda untuk mengelompokkan beberapa project yang saling berkaitan (misalnya, repository frontend dan repository backend yang terpisah). Dengan mengonfigurasi *project family*, sistem MCP akan mengaktifkan:

1. **Code Graph Gabungan:** Ketika `graph_build` dipanggil dengan parameter `family`, sistem akan membangun Code Graph gabungan lintas project, di mana setiap *node* akan ditandai secara eksplisit dengan `project_id::` masing-masing.
2. **Memori Episodik Bersama:** AI agent akan memiliki akses ke *Memory Bank* bersama untuk seluruh keluarga project (`family_<slug>`), memungkinkan agent untuk menggunakan kembali pola arsitektur, *business logic*, atau konvensi kode yang telah dipelajari dari semua project anggota.

## 👤 Siapa yang Menangani Konfigurasi?

> [!IMPORTANT]
> **Project Families harus dikonfigurasi secara manual oleh developer (Anda).**

Server MCP dan AI agent tidak akan secara otomatis menebak atau membuat konfigurasi ini, karena menentukan project mana yang saling berhubungan adalah keputusan arsitektur tingkat tinggi yang membutuhkan wawasan manusia.

> Setelah Anda mengatur file konfigurasi secara manual, sistem MCP secara otomatis mengambil alih dan menangani seluruh orkestrasi di belakang layar (penggabungan grafik, berbagi memori, sinkronisasi ID) secara otonom.

## 🛠️ Praktik Terbaik Konfigurasi

Praktik terbaik untuk mengonfigurasi *project family* adalah dengan membuat file deklarasi JSON secara manual di direktori konfigurasi AWLab global Anda.

### 🔖 1. 📂 Temukan atau Buat File Konfigurasi

File konfigurasi ini bernama `project-families.json` dan harus ditempatkan di direktori konfigurasi global Anda:

- **Windows:** `C:\Users\<username>\.awlab-id\agent-memory\project-families.json`
- **Mac/Linux:** `~/.awlab-id/agent-memory/project-families.json`

### 🔖 2. 📝 Tentukan Struktur JSON (format v2)

Gunakan format `v2` yang direkomendasikan untuk memetakan `family_slug` unik ke project-project anggotanya.

**Contoh `project-families.json`:**

```json
{
  "my_awesome_product": {
    "name": "My Awesome Product Suite",
    "members": [
      {
        "path": "D:/Project/IDE/my-frontend-app",
        "project_id": "frontend"
      },
      {
        "path": "D:/Project/IDE/my-backend-api",
        "project_id": "backend"
      }
    ]
  }
}
```

**Penjelasan Field:**

- `family_slug` (misalnya, `"my_awesome_product"`): Identifier unik untuk *project family*. Gunakan huruf kecil, angka, dan garis bawah (tanpa spasi).
- `name`: Nama yang mudah dibaca oleh manusia untuk keluarga project tersebut.
- `members`: Daftar project yang saling terhubung.
  - `path`: Path absolut ke direktori project di komputer lokal Anda. Gunakan garis miring biasa (`/`).
  - `project_id`: Nama pendek yang unik untuk project tersebut (misalnya, `"frontend"`). *Catatan: Jika project tersebut sudah memiliki file `.ai/project-id`, ID lokal tersebut akan dianggap sebagai sumber kebenaran (authoritative) dan akan menimpa ID yang dideklarasikan di file ini.*

### 🔖 3. 🤖 Penggunaan oleh AI Agent

Setelah file ini disimpan, Anda tidak perlu melakukan konfigurasi apa pun lagi.

Ketika AI agent berinteraksi dengan salah satu project ini, agent akan mendeteksi hubungan *family* tersebut secara dinamis. Agent kemudian dapat menggunakan tool seperti `graph_build` dengan `family="my_awesome_product"` untuk menghasilkan wawasan lintas repository, atau menggunakan `mem_search` dengan `store="family_my_awesome_product"` untuk mengingat konvensi arsitektur yang dibagikan.
