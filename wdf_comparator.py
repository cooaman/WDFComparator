#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WDF DataFlash Comparator (XML vs MOT) – Tkinter App (FINAL: 3-column compare)

Columns:
- XML Value OG: original value from XML (no transform)
- XML Value Expected: computed from XML only, using datatype-based swapping
    * byte / byteA  : no change
    * Word / WordA  : swap each 16-bit word (AB -> BA)
    * DWord / DWordA: reverse each 32-bit dword (ABCD -> DCBA) per 4-byte chunk
- Mot_Val: raw bytes from MOT (abs + offset .. length), NO transform

Result: OK if Mot_Val == XML Value Expected, else Fail

Other features:
- Offset parsed as HEX, interpreted ZERO-BASED for all types.
- PN dropdown from <pd part_number>; compare only mm_rdi under selected PN.
- <rdid> in XML is HEX -> parsed, shown as DEC for Excel mapping:
    DataFlashBank:  Col C (rdid DEC) -> Col D (Reference ID)
    Static:         Col B (Reference ID) -> Col C (addr), Col D (size WDF)
- Abs Address = 0xFF200000 + Static!C
- MOT read:
    * If size_wdf present: read block [Abs : Abs+Size(WDF)], slice [offset : offset+length]
    * Else: read at Abs + offset for length
- MOT Package: raw data bytes of the accumulated range (block if used, else the compared range)
- Row highlighting: OK → light green, Fail → light red
- CSV export

Requires:
    pip install pandas openpyxl
"""

import os, re, csv, binascii, difflib, tkinter as tk
from tkinter import ttk, filedialog, messagebox
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

try:
    import pandas as pd
except Exception:
    pd = None
try:
    import xml.etree.ElementTree as ET
except Exception:
    ET = None

BASE_ADDR = 0xFF200000

# ------------------------ Utilities ------------------------

def to_int_auto(x: Any) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, int):
        return x
    s = str(x).strip()
    if not s:
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        if re.fullmatch(r"[0-9a-fA-F]+", s) and not s.isdigit():
            return int(s, 16)
        return int(s)
    except Exception:
        return None

def parse_hex_str(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    t = s.strip().lower()
    if not t:
        return None
    if t.endswith('h') and t[:-1]:
        t = t[:-1]
    if t.startswith('0x'):
        t = t[2:]
    try:
        return int(t, 16)
    except Exception:
        return None

def hexstr_to_bytes(h: str) -> bytes:
    h = re.sub(r"[^0-9a-fA-F]", "", h or "")
    if len(h) % 2:
        h = "0" + h
    try:
        return binascii.unhexlify(h)
    except Exception:
        return b""

def bytes_to_hex(b: bytes) -> str:
    return binascii.hexlify(b).decode("ascii").upper()

def swap_bytes_in_words(b: bytes) -> bytes:
    """Swap bytes within each 16-bit word (AB -> BA)."""
    out = bytearray()
    for i in range(0, len(b), 2):
        chunk = b[i:i+2]
        out.extend(chunk[::-1] if len(chunk) == 2 else chunk)
    return bytes(out)

def swap_bytes_in_dwords(b: bytes) -> bytes:
    """Reverse bytes within each 32-bit dword (ABCD -> DCBA), per 4-byte chunk."""
    out = bytearray()
    for i in range(0, len(b), 4):
        chunk = b[i:i+4]
        out.extend(chunk[::-1] if len(chunk) == 4 else chunk)
    return bytes(out)

def normalize_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def localname(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag

def normalize_dtype(dt: Optional[str]) -> str:
    """Normalize 'Word', 'WORD', 'Word (2 bytes)' -> 'word' etc."""
    if not dt:
        return ""
    return re.sub(r"[^a-z0-9]", "", dt.lower())

# ------------------------ S-record image ------------------------

class SRecordImage:
    def __init__(self):
        self.data: Dict[int, int] = {}
        self.records: List[tuple[int, bytes, str]] = []

    def load(self, path: str) -> None:
        with open(path, "r", encoding="ascii", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw or not raw.startswith("S"):
                    continue
                t = raw[1]
                if t not in {"1", "2", "3"}:
                    continue
                count = int(raw[2:4], 16)
                payload = raw[4:4 + count * 2]
                by = binascii.unhexlify(payload)
                addr_len = 2 if t == "1" else 3 if t == "2" else 4
                addr = int.from_bytes(by[:addr_len], "big")
                data = by[addr_len:-1]  # last is checksum
                for i, b in enumerate(data):
                    self.data[addr + i] = b
                self.records.append((addr, data, raw))

    def read(self, addr: int, length: int) -> bytes:
        if addr is None or length is None or length <= 0:
            return b""
        return bytes(self.data.get(addr + i, 0xFF) for i in range(length))

# ------------------------ Excel Map ------------------------

@dataclass
class MapInfo:
    reference_id: Optional[str]
    static_addr: Optional[int]
    static_size_wdf: Optional[int]

class ExcelMap:
    def __init__(self):
        self.df_dataflash = None
        self.df_static = None

    def load(self, path: str) -> None:
        if pd is None:
            raise RuntimeError("pandas/openpyxl required: pip install pandas openpyxl")
        xl = pd.ExcelFile(path)

        def pick(name_like: str, default_idx: int):
            for s in xl.sheet_names:
                if s.lower() == name_like.lower():
                    return xl.parse(s, header=None)
            return xl.parse(xl.sheet_names[min(default_idx, len(xl.sheet_names) - 1)], header=None)

        self.df_dataflash = pick("DataFlashBank", 0)  # C: rdid DEC, D: ref ID
        self.df_static    = pick("Static", 1)         # B: ref ID, C: addr, D: size WDF

    def lookup_by_rdid_dec(self, rdid_dec: int) -> MapInfo:
        if self.df_dataflash is None or self.df_static is None:
            return MapInfo(None, None, None)

        df_dfb = self.df_dataflash
        def col(df, idx): return (lambda r: r.iloc[idx] if r.shape[0] > idx else None)

        match_dfb = df_dfb[df_dfb.apply(
            lambda r: (to_int_auto(col(df_dfb, 2)(r)) is not None and to_int_auto(col(df_dfb, 2)(r)) == rdid_dec),
            axis=1
        )]

        ref_id = None
        if not match_dfb.empty:
            r0 = match_dfb.iloc[0]
            ref_id = str(col(df_dfb, 3)(r0)).strip() if col(df_dfb, 3)(r0) is not None else None

        if not ref_id:
            return MapInfo(None, None, None)

        df_st = self.df_static
        def row_matches_static(r) -> bool:
            cB = r.iloc[1] if r.shape[0] > 1 else None
            if cB is None: return False
            sB = str(cB).strip()
            if sB == ref_id: return True
            try:
                return to_int_auto(sB) is not None and to_int_auto(sB) == to_int_auto(ref_id)
            except Exception:
                return False

        match_st = df_st[df_st.apply(row_matches_static, axis=1)]
        if match_st.empty:
            return MapInfo(ref_id, None, None)

        s0 = match_st.iloc[0]
        addr = to_int_auto(s0.iloc[2]) if s0.shape[0] > 2 else None
        size_wdf = to_int_auto(s0.iloc[3]) if s0.shape[0] > 3 else None
        return MapInfo(ref_id, addr, size_wdf)

# ------------------------ Test report parser ------------------------

def parse_test_report_params(path: str) -> List[str]:
    names: List[str] = []
    in_wdf = False
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("WDF read"):
                in_wdf = True
                continue
            if in_wdf and line.startswith("Read "):
                n = line.split(";", 1)[0].replace("Read ", "").strip()
                names.append(normalize_name(n))
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            uniq.append(n); seen.add(n)
    return uniq

# ------------------------ XML structures ------------------------

@dataclass
class XMLParam:
    name: str
    id_str: Optional[str]
    offset: Optional[int]          # HEX parsed (zero-based interpretation)
    length: Optional[int]
    value_bytes: bytes
    data_type: Optional[str]

class XMLCatalog:
    def __init__(self):
        self.pns: List[str] = []
        self._by_pn_by_name: Dict[str, Dict[str, XMLParam]] = {}
        self._by_pn_by_id: Dict[str, Dict[str, XMLParam]] = {}
        self.by_name: Dict[str, XMLParam] = {}
        self.by_id: Dict[str, XMLParam] = {}
        self.selected_pn: Optional[str] = None

    def load(self, path: str) -> None:
        if ET is None:
            raise RuntimeError("XML support unavailable")
        tree = ET.parse(path)
        root = tree.getroot()

        for el in root.iter():
            if localname(el.tag) != "pd":
                continue
            pn = el.attrib.get("part_number")
            if not pn:
                continue
            if pn not in self.pns:
                self.pns.append(pn)

            by_name, by_id = {}, {}
            for mm in el:
                if localname(mm.tag) != "mm_rdi":
                    continue
                name = normalize_name(mm.attrib.get("name", ""))
                if not name:
                    continue
                rdid = None
                off_txt = None
                data_txt, dt = "", None
                for ch in mm:
                    ln = localname(ch.tag)
                    if ln == "rdid":
                        rdid = (ch.text or "").strip()
                    elif ln == "offset":
                        off_txt = (ch.text or "").strip()
                    elif ln == "data":
                        data_txt = (ch.text or "").strip()
                        dt = (ch.attrib.get("type") or "").strip() if ch is not None else None
                value_bytes = hexstr_to_bytes(data_txt)
                offset = parse_hex_str(off_txt)                # HEX → int (zero-based)
                length = len(value_bytes) if value_bytes else None
                xp = XMLParam(name=name, id_str=rdid, offset=offset, length=length,
                              value_bytes=value_bytes, data_type=(dt or None))
                by_name[name] = xp
                if rdid:
                    by_id[rdid] = xp
            self._by_pn_by_name[pn] = by_name
            self._by_pn_by_id[pn] = by_id

        if self.pns:
            self.use_pn(self.pns[0])

    def use_pn(self, pn: str) -> None:
        if pn not in self._by_pn_by_name:
            raise ValueError(f"PN '{pn}' not found (available: {self.pns})")
        self.by_name = self._by_pn_by_name[pn]
        self.by_id = self._by_pn_by_id[pn]
        self.selected_pn = pn

    def find_by_name_fuzzy(self, name: str) -> Optional[XMLParam]:
        name = normalize_name(name)
        if name in self.by_name:
            return self.by_name[name]
        choices = list(self.by_name.keys())
        best = difflib.get_close_matches(name, choices, n=1, cutoff=0.7)
        return self.by_name.get(best[0]) if best else None

# ------------------------ Comparison ------------------------

@dataclass
class CompareItem:
    param_name: str
    id_dec: Optional[int]
    ref_id: Optional[str]
    xml_offset: Optional[int]
    xml_length: Optional[int]
    static_offset: Optional[int]
    abs_addr: Optional[int]
    size_wdf: Optional[int]
    xml_value_og: str          # EXACT as in XML
    xml_value_expected: str    # transformed per datatype (XML-only)
    mot_value: str             # raw bytes from MOT
    mot_package: str           # raw bytes of accumulated range
    result: Optional[str]
    note: str = ""

class Comparator:
    def __init__(self, xml: XMLCatalog, xmap: ExcelMap, srec: SRecordImage):
        self.xml = xml
        self.xmap = xmap
        self.srec = srec

    def _expected_from_xml(self, xml_bytes: bytes, dtype_norm: str) -> bytes:
        if dtype_norm in ("word", "worda"):
            return swap_bytes_in_words(xml_bytes)
        if dtype_norm in ("dword", "dworda"):
            return swap_bytes_in_dwords(xml_bytes)
        # byte / bytea / others -> as-is
        return xml_bytes

    def compare_param(self, pname: str) -> CompareItem:
        notes: List[str] = []
        xp = self.xml.find_by_name_fuzzy(pname)
        if xp is None:
            return CompareItem(pname, None, None, None, None, None, None, None,
                               "", "", "", None, "Not found in XML")

        # <rdid> HEX -> DEC
        dec_id = parse_hex_str(xp.id_str)
        ref_id, static_off, size_wdf = None, None, None
        if dec_id is not None:
            mi = self.xmap.lookup_by_rdid_dec(dec_id)
            ref_id = mi.reference_id
            static_off = mi.static_addr
            size_wdf = mi.static_size_wdf
            if static_off is None: notes.append("Static address missing (via ref ID lookup)")
            if ref_id is None:     notes.append("Reference ID not found in DataFlashBank")
        else:
            notes.append("XML rdid missing or not hex")

        dtype_norm = normalize_dtype(xp.data_type)
        xml_bytes = xp.value_bytes or b""
        xml_len = xp.length if xp.length is not None else len(xml_bytes)
        if not xml_len:
            notes.append("XML value/length missing")
            xml_len = 0

        # Build Expected from XML (datatype-only transform)
        xml_expected_bytes = self._expected_from_xml(xml_bytes, dtype_norm)

        # Absolute address
        abs_addr = BASE_ADDR + static_off if static_off is not None else None
        if abs_addr is None:
            notes.append("No absolute address (Static Column C missing)")

        # Read MOT bytes (ZERO-BASED offset for all types) - NO transform on mot_value
        mot_slice = b""
        used_block = False
        if abs_addr is not None and xml_len > 0:
            xml_off = xp.offset if xp.offset is not None else 0
            if size_wdf is not None:
                block = self.srec.read(abs_addr, size_wdf)
                start = max(0, xml_off)
                end = max(start, start + xml_len)
                mot_slice = block[start:end]
                used_block = True
            else:
                mot_slice = self.srec.read(abs_addr + xml_off, xml_len)

        # Stringify
        xml_og_hex = bytes_to_hex(xml_bytes) if xml_bytes else ""
        xml_exp_hex = bytes_to_hex(xml_expected_bytes) if xml_expected_bytes else ""
        mot_hex = bytes_to_hex(mot_slice) if mot_slice else ""

        # Compare Expected vs MOT
        result = None
        if xml_exp_hex and mot_hex:
            result = "OK" if xml_exp_hex.upper() == mot_hex.upper() else "Fail"

        # MOT Package = accumulated RAW bytes (block if used, else compared range)
        if abs_addr is not None and xml_len > 0:
            if used_block:
                pkg_bytes = self.srec.read(abs_addr, size_wdf or 0)
            else:
                pkg_bytes = self.srec.read(abs_addr + (xp.offset or 0), xml_len)
            pkg_hex = bytes_to_hex(pkg_bytes)
            if len(pkg_hex) > 8000:
                pkg_hex = pkg_hex[:8000] + f"...(total {len(pkg_bytes)} bytes)"
        else:
            pkg_hex = ""

        if self.xml.selected_pn:
            notes.insert(0, f"PN={self.xml.selected_pn}")

        return CompareItem(
            param_name=pname,
            id_dec=dec_id,
            ref_id=ref_id,
            xml_offset=xp.offset,
            xml_length=xml_len,
            static_offset=static_off,
            abs_addr=abs_addr,
            size_wdf=size_wdf,
            xml_value_og=xml_og_hex,
            xml_value_expected=xml_exp_hex,
            mot_value=mot_hex,
            mot_package=pkg_hex,
            result=result,
            note="; ".join(notes)
        )

# ------------------------ Tkinter UI ------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WDF DataFlash Comparator (XML vs MOT)")
        self.geometry("1480x800")

        self.var_xml = tk.StringVar()
        self.var_txt = tk.StringVar()
        self.var_xlsx = tk.StringVar()
        self.var_mot = tk.StringVar()

        self.xmlcat: Optional[XMLCatalog] = None
        self.results: List[CompareItem] = []

        top = ttk.LabelFrame(self, text="Select Files")
        top.pack(fill=tk.X, padx=10, pady=8)
        self._file_row(top, "XML file:", self.var_xml, ("XML files", "*.xml"))
        self._file_row(top, "Test report (TXT):", self.var_txt, ("Text files", "*.txt"))
        self._file_row(top, "Excel map (XLSX):", self.var_xlsx, ("Excel files", "*.xlsx"))
        self._file_row(top, "MOT file:", self.var_mot, ("MOT/SREC", "*.mot *.s19 *.s28 *.s37 *.srec *.sre"))

        pn_row = ttk.Frame(self)
        pn_row.pack(fill=tk.X, padx=10, pady=(0, 6))
        ttk.Label(pn_row, text="PN (<pd part_number>):", width=22).pack(side=tk.LEFT)
        self.pn_var = tk.StringVar()
        self.pn_combo = ttk.Combobox(pn_row, textvariable=self.pn_var, values=[], state="disabled")
        self.pn_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(pn_row, text="Load XML & List PN", command=self.load_xml_and_list_pn).pack(side=tk.LEFT, padx=6)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10, pady=6)
        ttk.Button(btns, text="Load & Analyze", command=self.load_and_analyze).pack(side=tk.LEFT)
        ttk.Button(btns, text="Export CSV", command=self.export_csv).pack(side=tk.LEFT, padx=6)

        cols = (
            "param", "id_dec", "ref_id", "xml_off", "xml_len",
            "static_off", "abs_addr", "size_wdf",
            "xml_og", "xml_expected", "mot_val",
            "mot_package", "result", "note"
        )
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=24)
        for c, w in [
            ("param", 260), ("id_dec", 90), ("ref_id", 120), ("xml_off", 110),
            ("xml_len", 80), ("static_off", 130), ("abs_addr", 150), ("size_wdf", 110),
            ("xml_og", 200), ("xml_expected", 220), ("mot_val", 220),
            ("mot_package", 460), ("result", 80), ("note", 320)
        ]:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        self.tree.tag_configure("ok", background="#ccffcc")
        self.tree.tag_configure("fail", background="#ffcccc")

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, anchor=tk.W).pack(fill=tk.X, padx=10, pady=(0, 10))

    def _file_row(self, parent, label, var, filetype):
        row = ttk.Frame(parent); row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text=label, width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        def browse():
            path = filedialog.askopenfilename(title=f"Select {label}", filetypes=[filetype, ("All files", "*.*")])
            if path:
                var.set(path)
                if label.startswith("XML"):
                    try:
                        self.xmlcat = XMLCatalog()
                        self.xmlcat.load(path)
                        pns = self.xmlcat.pns or []
                        self.pn_combo.configure(state="readonly", values=pns if pns else [])
                        if pns: self.pn_combo.current(0)
                    except Exception as e:
                        messagebox.showerror("XML load", str(e))
        ttk.Button(row, text="Browse", command=browse).pack(side=tk.LEFT, padx=6)

    def load_xml_and_list_pn(self):
        xml_path = self.var_xml.get().strip()
        if not os.path.isfile(xml_path):
            messagebox.showerror("Missing file", "Please select the XML file first.")
            return
        try:
            self.xmlcat = XMLCatalog()
            self.xmlcat.load(xml_path)
            pns = self.xmlcat.pns or []
            self.pn_combo.configure(state="readonly", values=pns if pns else [])
            if pns:
                self.pn_combo.current(0)
            messagebox.showinfo("PNs loaded", f"Found {len(pns)} PN option(s).")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to parse PN from XML: {e}")

    def load_and_analyze(self):
        xml_path = self.var_xml.get().strip()
        txt_path = self.var_txt.get().strip()
        xlsx_path = self.var_xlsx.get().strip()
        mot_path = self.var_mot.get().strip()

        if not (os.path.isfile(xml_path) and os.path.isfile(txt_path) and os.path.isfile(xlsx_path) and os.path.isfile(mot_path)):
            messagebox.showerror("Missing files", "Please select all four files (XML, TXT, XLSX, MOT).")
            return

        try:
            self.status.set("Parsing MOT..."); self.update_idletasks()
            srec = SRecordImage(); srec.load(mot_path)

            self.status.set("Loading Excel map..."); self.update_idletasks()
            xmap = ExcelMap(); xmap.load(xlsx_path)

            self.status.set("Parsing XML..."); self.update_idletasks()
            if not self.xmlcat:
                self.xmlcat = XMLCatalog(); self.xmlcat.load(xml_path)

            selected_pn = (self.pn_var.get().strip() if self.pn_var.get() else (self.xmlcat.pns[0] if self.xmlcat.pns else None))
            if selected_pn:
                try:
                    self.xmlcat.use_pn(selected_pn)
                except Exception as e:
                    messagebox.showerror("PN error", str(e)); return

            self.status.set("Reading test report..."); self.update_idletasks()
            want_params = parse_test_report_params(txt_path)
            if want_params:
                want_params = [p for p in want_params if p in self.xmlcat.by_name]
            if not want_params:
                want_params = list(self.xmlcat.by_name.keys())[:1000]

            self.status.set(f"Comparing {len(want_params)} parameter(s)..."); self.update_idletasks()
            comp = Comparator(self.xmlcat, xmap, srec)
            self.results = [comp.compare_param(pn) for pn in want_params]

            # refresh table
            for i in self.tree.get_children():
                self.tree.delete(i)

            ok_count, fail_count, unk = 0, 0, 0
            for r in self.results:
                vals = (
                    r.param_name,
                    r.id_dec if r.id_dec is not None else "",
                    r.ref_id or "",
                    f"0x{r.xml_offset:X}" if r.xml_offset is not None else "",
                    r.xml_length if r.xml_length is not None else "",
                    f"{r.static_offset}" if r.static_offset is not None else "",
                    f"0x{r.abs_addr:X}" if r.abs_addr is not None else "",
                    r.size_wdf if r.size_wdf is not None else "",
                    r.xml_value_og,
                    r.xml_value_expected,
                    r.mot_value,
                    r.mot_package,
                    r.result or "?",
                    r.note
                )
                tag = "ok" if r.result == "OK" else ("fail" if r.result == "Fail" else "")
                self.tree.insert("", tk.END, values=vals, tags=(tag,))
                if r.result == "OK": ok_count += 1
                elif r.result == "Fail": fail_count += 1
                else: unk += 1

            self.status.set(f"Done. OK: {ok_count}  Fail: {fail_count}  Unknown: {unk}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.status.set("Error.")

    def export_csv(self):
        if not self.results:
            messagebox.showinfo("Nothing to export", "Run an analysis first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "Parameter","ID (DEC)","Reference ID","XML Offset","XML Length",
                "Static Offset (DEC)","Abs Address (HEX)","Size (WDF)",
                "XML Value OG","XML Value Expected","Mot_Val","MOT Package","Result","Note"
            ])
            for r in self.results:
                w.writerow([
                    r.param_name,
                    r.id_dec if r.id_dec is not None else "",
                    r.ref_id or "",
                    f"0x{r.xml_offset:X}" if r.xml_offset is not None else "",
                    r.xml_length if r.xml_length is not None else "",
                    r.static_offset if r.static_offset is not None else "",
                    f"0x{r.abs_addr:X}" if r.abs_addr is not None else "",
                    r.size_wdf if r.size_wdf is not None else "",
                    r.xml_value_og,
                    r.xml_value_expected,
                    r.mot_value,
                    r.mot_package,
                    r.result or "?",
                    r.note
                ])
        messagebox.showinfo("Exported", f"Saved: {path}")

# ------------------------ Main ------------------------

if __name__ == "__main__":
    App().mainloop()