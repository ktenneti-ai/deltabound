// Main-thread client for py.worker.ts.
import type { Request, Response } from "./py.worker";
import type { InputFile, Lineage, Payload } from "./types";

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void };
type DistributiveOmit<T, K extends keyof never> = T extends unknown ? Omit<T, K> : never;

export class PythonClient {
  private worker = new Worker(new URL("./py.worker.ts", import.meta.url), { type: "module" });
  private next = 1;
  private pending = new Map<number, Pending>();
  onProgress: (message: string) => void = () => {};

  constructor() {
    this.worker.onmessage = (e: MessageEvent<Response>) => {
      const msg = e.data;
      if ("progress" in msg) return this.onProgress(msg.progress);
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.ok) p.resolve(msg.result);
      else p.reject(new Error(msg.error));
    };
    this.worker.onerror = (e) => {
      for (const p of this.pending.values()) p.reject(new Error(e.message || "Python worker failed"));
      this.pending.clear();
    };
  }

  private call<T>(req: DistributiveOmit<Request, "id">, transfer: Transferable[] = []): Promise<T> {
    const id = this.next++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.worker.postMessage({ ...req, id }, transfer);
    });
  }

  terminate(): void {
    this.worker.terminate();
    for (const p of this.pending.values()) p.reject(new Error("Python worker stopped"));
    this.pending.clear();
  }

  init(): Promise<string> {
    return this.call({ op: "init", base: new URL(".", document.baseURI).href });
  }

  load(files: InputFile[]): Promise<Payload> {
    return this.call({ op: "load", files }, files.map((f) => f.data));
  }

  lineage(dataflow: string): Promise<Lineage> {
    return this.call({ op: "lineage", dataflow });
  }
}
