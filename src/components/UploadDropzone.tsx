import * as React from "react";
import { FileImage, Upload, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type FileLike = {
  name: string;
  size: number;
};

interface UploadDropzoneProps {
  value: FileLike | null;
  onChange: (file: FileLike | null) => void;
}

const acceptedTypes = ".jpg,.jpeg,.png,.webp";

export function UploadDropzone({ value, onChange }: UploadDropzoneProps) {
  const [dragActive, setDragActive] = React.useState(false);

  const handleNativeFile = (file: File) => {
    onChange({ name: file.name, size: file.size });
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const file = event.dataTransfer.files?.[0];
    if (file) {
      handleNativeFile(file);
    }
  };

  return (
    <div className="space-y-3">
      <div
        onDragEnter={() => setDragActive(true)}
        onDragLeave={() => setDragActive(false)}
        onDragOver={(event) => event.preventDefault()}
        onDrop={handleDrop}
        className={cn(
          "relative overflow-hidden rounded-2xl border border-dashed border-slate-300 p-7 text-center transition-all duration-200",
          dragActive
            ? "border-blue-500 bg-blue-50/60 shadow-[0_10px_28px_rgba(37,99,235,0.12)]"
            : "bg-slate-50/60 hover:border-slate-400"
        )}
      >
        <div className="absolute inset-x-0 top-0 h-1 bg-gradient-to-r from-transparent via-blue-300/60 to-transparent" />
        <Upload className="mx-auto mb-2 h-5 w-5 text-blue-600" />
        <p className="text-sm font-semibold text-slate-800">Drop imaging files to create a case</p>
        <p className="mt-1 text-xs text-slate-500">Accepted formats: .jpg, .jpeg, .png, .webp</p>
        <label className="mt-4 inline-block cursor-pointer rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50">
          Browse files
          <input
            type="file"
            accept={acceptedTypes}
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                handleNativeFile(file);
              }
            }}
          />
        </label>
      </div>

      {value ? (
        <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-3 py-2.5">
          <div className="flex items-center gap-2">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-blue-50 text-blue-700">
              <FileImage className="h-4 w-4" />
            </span>
            <div>
              <p className="text-sm font-medium text-slate-900">{value.name}</p>
              <p className="text-xs text-slate-500">{(value.size / 1024).toFixed(1)} KB</p>
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={() => onChange(null)}>
            <X className="h-4 w-4" />
            Remove
          </Button>
        </div>
      ) : null}
    </div>
  );
}
