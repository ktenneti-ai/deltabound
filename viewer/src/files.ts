// Read user files: picker (files or a folder), drag and drop (folders recursively),
// demo files and ?src= URLs. Contents never leave the browser.
import type { InputFile } from "./types";

export async function fromFileList(list: FileList | File[]): Promise<InputFile[]> {
  return Promise.all(
    Array.from(list).map(async (f) => ({ name: f.webkitRelativePath || f.name, data: await f.arrayBuffer() })),
  );
}

function readEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function fromEntry(entry: FileSystemEntry): Promise<InputFile[]> {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
    return [{ name: entry.fullPath.replace(/^\//, ""), data: await file.arrayBuffer() }];
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  const out: InputFile[] = [];
  // readEntries returns results in batches until it returns an empty array.
  for (let batch = await readEntries(reader); batch.length; batch = await readEntries(reader)) {
    for (const child of batch) out.push(...(await fromEntry(child)));
  }
  return out;
}

export async function fromDrop(dt: DataTransfer): Promise<InputFile[]> {
  const entries = Array.from(dt.items)
    .map((i) => i.webkitGetAsEntry?.())
    .filter((e): e is FileSystemEntry => !!e);
  if (!entries.length) return fromFileList(dt.files);
  return (await Promise.all(entries.map(fromEntry))).flat();
}

export async function fromUrls(urls: string[]): Promise<InputFile[]> {
  return Promise.all(
    urls.map(async (url) => {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Could not fetch ${url}: HTTP ${res.status}`);
      return { name: decodeURIComponent(new URL(url, document.baseURI).pathname.split("/").pop() || url), data: await res.arrayBuffer() };
    }),
  );
}

export async function demoFiles(): Promise<InputFile[]> {
  const res = await fetch("demo/manifest.json");
  if (!res.ok) throw new Error("Demo files are missing; run `npm run prepare:py`.");
  const { files } = (await res.json()) as { files: string[] };
  return fromUrls(files.map((f) => `demo/${f}`));
}
