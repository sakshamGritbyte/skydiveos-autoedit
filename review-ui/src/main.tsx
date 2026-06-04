import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { EdlReviewScreen } from "./EdlReviewScreen";
import { ApiError, ReviewApi } from "./api";
import type { EditDecisionList, JobResponse } from "./types";
import "./index.css";

// Same-origin client: requests go through the Vite proxy (vite.config.ts) to the
// real FastAPI backend, so there's no mock and no CORS. Override the origin with
// VITE_API_BASE if you serve the API somewhere the proxy doesn't cover.
const api = new ReviewApi(import.meta.env.VITE_API_BASE ?? "");

// Which job to review: ?job=<id> in the URL (the SkydiveOS shell would route this).
const jobId = new URLSearchParams(window.location.search).get("job");

type Loaded = { job: JobResponse; edl: EditDecisionList };

function App() {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    const ac = new AbortController();
    setError(null);
    // The timeline needs both the job state and its composed EDL.
    Promise.all([api.getJob(jobId, ac.signal), api.getEdl(jobId, ac.signal)])
      .then(([job, edl]) => setData({ job, edl }))
      .catch((e: unknown) => {
        if (ac.signal.aborted) return;
        setError(
          e instanceof ApiError
            ? e.status === 404
              ? "Job or its EDL not found — has it finished composing?"
              : e.message
            : "Could not reach the API. Is the backend running on :8000?",
        );
      });
    return () => ac.abort();
  }, []);

  if (!jobId) {
    return (
      <div className="mx-auto max-w-md p-8 text-sm text-muted-foreground">
        <h1 className="mb-2 text-lg font-semibold text-foreground">No job selected</h1>
        Append <code className="rounded bg-muted px-1">?job=&lt;job_id&gt;</code> to the URL —
        e.g. <code className="rounded bg-muted px-1">/?job=abc123</code>. Create one with{" "}
        <code className="rounded bg-muted px-1">POST /jobs</code> and upload footage so the
        pipeline renders a preview to review.
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-md p-8 text-sm">
        <h1 className="mb-2 text-lg font-semibold">Couldn't load job</h1>
        <p className="text-destructive">{error}</p>
        <code className="mt-2 block text-xs text-muted-foreground">job {jobId}</code>
      </div>
    );
  }

  if (!data) {
    return <div className="mx-auto max-w-md p-8 text-sm text-muted-foreground">Loading job {jobId}…</div>;
  }

  return (
    <EdlReviewScreen
      jobId={data.job.job_id}
      edl={data.edl}
      job={data.job}
      api={api}
      onActed={(action) => {
        // After approve/reject the edit leaves the review gate; reflect that.
        if (action !== "tweak") window.location.reload();
      }}
    />
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
