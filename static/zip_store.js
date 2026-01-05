/* global window */

// Minimal ZIP writer (store-only, no compression) for in-browser exports.
// Produces a valid .zip with UTF-8 filenames.
//
// Usage:
//   const z = new window.ZipStore();
//   z.addText("snapshot.md", "# ...");
//   z.addBytes("payload.json", new TextEncoder().encode("{...}"));
//   const blob = z.toBlob();

(function () {
  function _u32(n) {
    const x = Number(n) >>> 0;
    return new Uint8Array([x & 255, (x >>> 8) & 255, (x >>> 16) & 255, (x >>> 24) & 255]);
  }
  function _u16(n) {
    const x = Number(n) & 0xffff;
    return new Uint8Array([x & 255, (x >>> 8) & 255]);
  }

  // CRC32
  const _crcTable = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      t[i] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xffffffff;
    const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
    for (let i = 0; i < b.length; i++) {
      c = _crcTable[(c ^ b[i]) & 255] ^ (c >>> 8);
    }
    return (c ^ 0xffffffff) >>> 0;
  }

  function concat(chunks) {
    const xs = chunks.filter(Boolean);
    const total = xs.reduce((a, x) => a + (x.byteLength || x.length || 0), 0);
    const out = new Uint8Array(total);
    let off = 0;
    for (const x of xs) {
      const b = x instanceof Uint8Array ? x : new Uint8Array(x);
      out.set(b, off);
      off += b.length;
    }
    return out;
  }

  function dosDateTime(d) {
    const dt = d instanceof Date ? d : new Date();
    const year = Math.max(1980, dt.getFullYear());
    const month = dt.getMonth() + 1;
    const day = dt.getDate();
    const hour = dt.getHours();
    const min = dt.getMinutes();
    const sec = Math.floor(dt.getSeconds() / 2); // 2-second increments
    const dosTime = (hour << 11) | (min << 5) | sec;
    const dosDate = ((year - 1980) << 9) | (month << 5) | day;
    return { dosTime, dosDate };
  }

  class ZipStore {
    constructor() {
      this._files = [];
      this._createdAt = new Date();
    }

    addBytes(path, bytes) {
      const name = String(path || "").replace(/^[\\/]+/, "");
      const data = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
      this._files.push({ name, data });
    }

    addText(path, text) {
      const enc = new TextEncoder();
      this.addBytes(path, enc.encode(String(text ?? "")));
    }

    toUint8Array() {
      const enc = new TextEncoder();
      const { dosTime, dosDate } = dosDateTime(this._createdAt);

      const localParts = [];
      const centralParts = [];
      let offset = 0;

      for (const f of this._files) {
        const nameBytes = enc.encode(String(f.name || "file"));
        const data = f.data || new Uint8Array();
        const c = crc32(data);
        const size = data.length >>> 0;

        // Local file header
        // signature 0x04034b50
        const localHeader = concat([
          _u32(0x04034b50),
          _u16(20), // version needed
          _u16(0x0800), // flags (UTF-8)
          _u16(0), // compression method = store
          _u16(dosTime),
          _u16(dosDate),
          _u32(c),
          _u32(size),
          _u32(size),
          _u16(nameBytes.length),
          _u16(0), // extra len
          nameBytes,
        ]);
        localParts.push(localHeader, data);

        // Central directory header
        // signature 0x02014b50
        const centralHeader = concat([
          _u32(0x02014b50),
          _u16(20), // version made by
          _u16(20), // version needed
          _u16(0x0800), // flags UTF-8
          _u16(0), // store
          _u16(dosTime),
          _u16(dosDate),
          _u32(c),
          _u32(size),
          _u32(size),
          _u16(nameBytes.length),
          _u16(0), // extra
          _u16(0), // comment
          _u16(0), // disk start
          _u16(0), // internal attrs
          _u32(0), // external attrs
          _u32(offset),
          nameBytes,
        ]);
        centralParts.push(centralHeader);

        offset += localHeader.length + data.length;
      }

      const centralDir = concat(centralParts);
      const centralOffset = offset;
      const centralSize = centralDir.length;

      // End of central directory record
      // signature 0x06054b50
      const end = concat([
        _u32(0x06054b50),
        _u16(0), // disk
        _u16(0), // start disk
        _u16(this._files.length),
        _u16(this._files.length),
        _u32(centralSize),
        _u32(centralOffset),
        _u16(0), // comment len
      ]);

      return concat([...localParts, centralDir, end]);
    }

    toBlob() {
      const bytes = this.toUint8Array();
      return new Blob([bytes], { type: "application/zip" });
    }
  }

  window.ZipStore = ZipStore;
})();


