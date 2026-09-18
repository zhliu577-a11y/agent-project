import {
  CheckCircle2,
  FileArchive,
  PackagePlus,
  ScanSearch,
  UploadCloud,
  X
} from "lucide-react";
import { useRef, useState } from "react";

import { inspectPlugin, installPlugin } from "../api/client";
import type { PackageInspection } from "../api/types";

interface PluginUploadPanelProps {
  onInstalled: (restartRequired: boolean) => Promise<void>;
  onNotify: (message: string, tone?: "success" | "error" | "info") => void;
}

export function PluginUploadPanel({ onInstalled, onNotify }: PluginUploadPanelProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [inspection, setInspection] = useState<PackageInspection | null>(null);
  const [busy, setBusy] = useState<"inspect" | "install" | null>(null);
  const [dragging, setDragging] = useState(false);
  const [installed, setInstalled] = useState(false);

  const chooseFile = (next: File | null) => {
    if (!next) {
      return;
    }
    setFile(next);
    setInspection(null);
    setInstalled(false);
  };

  const inspect = async () => {
    if (!file) {
      return;
    }
    setBusy("inspect");
    try {
      const result = await inspectPlugin(file);
      setInspection(result);
      onNotify(`校验通过：${result.name}`, "success");
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "插件检查失败", "error");
    } finally {
      setBusy(null);
    }
  };

  const install = async () => {
    if (!file) {
      return;
    }
    setBusy("install");
    try {
      const result = await installPlugin(file);
      setInstalled(true);
      onNotify(`已安装 ${result.plugin.name}，当前为禁用状态`, "success");
      await onInstalled(result.restartRequired);
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "插件安装失败", "error");
    } finally {
      setBusy(null);
    }
  };

  const reset = () => {
    setFile(null);
    setInspection(null);
    setInstalled(false);
    if (inputRef.current) {
      inputRef.current.value = "";
    }
  };

  return (
    <section className="panel upload-panel">
      <div className="panel__header">
        <div>
          <span className="section-kicker">Package</span>
          <h3>安装功能包</h3>
        </div>
        {file ? (
          <button className="icon-button" type="button" title="清除文件" onClick={reset}>
            <X size={17} />
          </button>
        ) : (
          <FileArchive size={18} />
        )}
      </div>

      {!file ? (
        <button
          className={`drop-zone ${dragging ? "is-dragging" : ""}`}
          type="button"
          onClick={() => inputRef.current?.click()}
          onDragEnter={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            chooseFile(event.dataTransfer.files[0] ?? null);
          }}
        >
          <UploadCloud size={24} />
          <strong>拖入插件 zip，或选择文件</strong>
          <span>最大 50 MB</span>
        </button>
      ) : (
        <div className="selected-file">
          <div className="selected-file__icon">
            <FileArchive size={20} />
          </div>
          <div className="selected-file__body">
            <strong>{file.name}</strong>
            <span>{formatFileSize(file.size)}</span>
          </div>
          {installed ? <CheckCircle2 size={20} className="text-success" /> : null}
        </div>
      )}

      <input
        ref={inputRef}
        className="visually-hidden"
        type="file"
        accept=".zip,application/zip"
        onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
      />

      {file && !inspection ? (
        <button
          className="button button--primary button--block"
          type="button"
          disabled={busy !== null}
          onClick={() => void inspect()}
        >
          <ScanSearch size={16} />
          {busy === "inspect" ? "检查中…" : "检查插件包"}
        </button>
      ) : null}

      {inspection ? (
        <div className="inspection">
          <div className="inspection__title">
            <div>
              <strong>{inspection.name}</strong>
              <span>{inspection.version || "unversioned"}</span>
            </div>
            <span className="badge">{inspection.contributions.length} contributions</span>
          </div>
          {inspection.description ? <p>{inspection.description}</p> : null}
          <div className="contribution-list">
            {inspection.contributions.map((contribution) => (
              <div
                className="contribution-row"
                key={`${contribution.kind}:${contribution.name}:${contribution.id}`}
              >
                <span className="kind-tag">{contribution.kind}</span>
                <div>
                  <strong>{contribution.name}</strong>
                  <span>{contribution.contract || "contract: default"}</span>
                </div>
                {contribution.requires.length ? (
                  <small>{contribution.requires.length} deps</small>
                ) : null}
              </div>
            ))}
          </div>
          <button
            className="button button--primary button--block"
            type="button"
            disabled={busy !== null || installed}
            onClick={() => void install()}
          >
            {installed ? <CheckCircle2 size={16} /> : <PackagePlus size={16} />}
            {installed ? "已安装" : busy === "install" ? "安装中…" : "安装到 Registry"}
          </button>
        </div>
      ) : null}
    </section>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
