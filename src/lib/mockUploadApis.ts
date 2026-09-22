import { API_BASE } from "./api";

export interface MatchItem {
  score: number;
  diagnosis: string;
  summary: string;
  facility: string;
  outcome: string;
  outcomeVariant: "success" | "warning" | "neutral";
  image_url: string;
  asset_id?: string;
  related_image_urls?: string[];
  age?: number;
  gender?: string;
  pmc_id?: string;
  article_title?: string;
  journal?: string;
  year?: string;
  radiology_view?: string;
  case_text?: string;
  raw_payload?: Record<string, any>;
}

type JsonRecord = Record<string, unknown>;

const PROFILE_ARRAY_PATHS = [
  ["patient", "comorbidities"], ["patient", "medications"],
  ["assessment", "suspected_primary"], ["assessment", "differential"],
  ["assessment", "diagnosis_secondary"],
  ["findings", "lungs", "consolidation_locations"],
  ["findings", "lungs", "atelectasis_locations"],
  ["findings", "devices", "device_list"], ["findings", "other"],
  ["summary", "key_points"], ["summary", "red_flags"],
  ["presentation", "differential_diagnosis"],
  ["plan", "immediate_interventions"], ["plan", "monitoring_recommendations"],
  ["provenance", "authors"],
  ["tags", "ml_labels"], ["tags", "gt_labels"], ["tags", "keywords"], ["tags", "mesh_terms"],
] as const;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Make cached/local-model payloads safe for list rendering in the UI. */
function normalizeMatchPayload(payload: unknown): Record<string, any> | undefined {
  if (!isRecord(payload)) return undefined;
  const normalized: JsonRecord = { ...payload };

  for (const path of PROFILE_ARRAY_PATHS) {
    let current: JsonRecord = normalized;
    let completePath = true;
    for (const key of path.slice(0, -1)) {
      const next = current[key];
      if (!isRecord(next)) {
        completePath = false;
        break;
      }
      current[key] = { ...next };
      current = current[key] as JsonRecord;
    }
    if (!completePath) continue;
    const field = path[path.length - 1];
    if (field in current && current[field] != null && !Array.isArray(current[field])) {
      current[field] = [current[field]];
    }
  }

  if ("related_images" in normalized && normalized.related_images != null && !Array.isArray(normalized.related_images)) {
    normalized.related_images = [normalized.related_images];
  }
  return normalized as Record<string, any>;
}

function normalizeMatch(match: MatchItem): MatchItem {
  return { ...match, raw_payload: normalizeMatchPayload(match.raw_payload) };
}

import type { CaseProfile } from "./caseProfileTypes";

export async function searchByImage(file: File, profile?: CaseProfile, limit = 10): Promise<MatchItem[]> {
  const formData = new FormData();
  formData.append("file", file);
  if (profile) {
    formData.append("profile", JSON.stringify(profile));
  }
  formData.append("limit", String(limit));

  const response = await fetch(`${API_BASE}/search?limit=${limit}`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let errMessage = `Search failed (${response.status})`;
    try {
      const data = await response.json();
      if (data.detail) {
        errMessage = data.detail;
      } else if (data.error) {
        errMessage = data.error;
      } else {
        errMessage = JSON.stringify(data);
      }
    } catch (e) {
      errMessage = await response.text() || errMessage;
    }
    throw new Error(errMessage);
  }

  const data = await response.json() as { matches: MatchItem[]; count: number };
  // The backend normally supplies canonical arrays. This guard keeps one old
  // or malformed cached enrichment from taking down the complete results view.
  return data.matches.map(normalizeMatch);
}

export interface ComparisonInsights {
  insights_text: string;
  original_box: [number, number, number, number]; // [ymin, xmin, ymax, xmax] max=1000
  match_box: [number, number, number, number];
}

export async function compareInsights(originalImage: File, matchItem: MatchItem): Promise<ComparisonInsights> {
  const formData = new FormData();
  formData.append("original_image", originalImage);
  formData.append("match_diagnosis", matchItem.diagnosis);
  if (matchItem.asset_id) {
    formData.append("match_asset_id", matchItem.asset_id);
  }
  if (matchItem.raw_payload) {
    formData.append("match_payload", JSON.stringify(matchItem.raw_payload));
  }

  const response = await fetch(`${API_BASE}/compare_insights`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let errMessage = `Comparison failed (${response.status})`;
    try {
      const data = await response.json();
      errMessage = data.detail || data.error || JSON.stringify(data);
    } catch {
      errMessage = await response.text() || errMessage;
    }
    throw new Error(errMessage);
  }

  return response.json() as Promise<ComparisonInsights>;
}

export interface CaseCardDraft {
  patientAge: string;
  patientSex: string;
  presentingConcern: string;
  currentSymptoms: string[];
  suspectedDiagnosis: string;
  imagingModality: string;
  imagingFileName: string;
  clinicalSummary: string;
  vitals: string;
  allergies: string;
}

export interface MockAgentResponse {
  reply: string;
  patch: Partial<CaseCardDraft>;
}

interface CompletionField {
  key: keyof CaseCardDraft;
  label: string;
  check: (value: CaseCardDraft[keyof CaseCardDraft]) => boolean;
}

const completionFields: CompletionField[] = [
  { key: "patientAge", label: "Patient age", check: (value) => Boolean(value) },
  { key: "patientSex", label: "Patient sex", check: (value) => Boolean(value) },
  { key: "presentingConcern", label: "Primary concern", check: (value) => Boolean(value) },
  { key: "currentSymptoms", label: "Symptoms", check: (value) => Array.isArray(value) && value.length > 0 },
  { key: "suspectedDiagnosis", label: "Suspected diagnosis", check: (value) => Boolean(value) },
  { key: "imagingModality", label: "Imaging modality", check: (value) => Boolean(value) },
  { key: "imagingFileName", label: "Imaging file", check: (value) => Boolean(value) },
  { key: "clinicalSummary", label: "Clinical summary", check: (value) => Boolean(value) }
];

const symptomDictionary: Array<[RegExp, string]> = [
  [/hemoptysis/i, "Hemoptysis"],
  [/weight loss/i, "Weight loss"],
  [/dyspnea|shortness of breath/i, "Dyspnea"],
  [/cough/i, "Persistent cough"],
  [/chest pain/i, "Chest pain"],
  [/fever/i, "Fever"],
  [/fatigue/i, "Fatigue"],
  [/headache/i, "Headache"]
];

export const emptyCaseCardDraft: CaseCardDraft = {
  patientAge: "",
  patientSex: "",
  presentingConcern: "",
  currentSymptoms: [],
  suspectedDiagnosis: "",
  imagingModality: "",
  imagingFileName: "",
  clinicalSummary: "",
  vitals: "",
  allergies: ""
};

export function computeCaseCompletion(draft: CaseCardDraft) {
  const completed = completionFields.filter((field) => field.check(draft[field.key])).length;
  const total = completionFields.length;
  const missing = completionFields
    .filter((field) => !field.check(draft[field.key]))
    .map((field) => field.label);

  return {
    score: Math.round((completed / total) * 100),
    completed,
    total,
    missing
  };
}

export async function mockIngestImagingFile(fileName: string): Promise<MockAgentResponse> {
  await delay(700);

  const lower = fileName.toLowerCase();
  const modality = lower.endsWith(".png") || lower.endsWith(".jpg") || lower.endsWith(".jpeg") || lower.endsWith(".webp")
      ? "Chest image upload"
      : "Imaging uploaded";

  return {
    reply:
      "I parsed the upload metadata and updated the imaging section in the case card. Add notes for stronger clinical confidence.",
    patch: {
      imagingFileName: fileName,
      imagingModality: modality
    }
  };
}

export async function mockAnalyzeClinicalNote(note: string): Promise<MockAgentResponse> {
  await delay(1000);

  const patch = extractPatchFromText(note);
  const completion = Object.keys(patch).length;

  return {
    reply:
      completion > 0
        ? `Analyzed note and mapped ${completion} field${completion === 1 ? "" : "s"} to the case card.`
        : "I could not map this note yet. Try adding age, symptoms, and suspected diagnosis in plain language.",
    patch
  };
}

export async function mockRespondToAgent(
  userMessage: string,
  currentDraft: CaseCardDraft
): Promise<MockAgentResponse> {
  await delay(800);

  if (/what is missing|what's missing|missing fields|what else/i.test(userMessage)) {
    const completion = computeCaseCompletion(currentDraft);
    if (completion.missing.length === 0) {
      return {
        reply: "Required sections are complete for this draft. You can proceed to review.",
        patch: {}
      };
    }

    return {
      reply: `Still missing: ${completion.missing.join(", ")}.`,
      patch: {}
    };
  }

  const patch = extractPatchFromText(userMessage);
  const mappedCount = Object.keys(patch).length;

  if (mappedCount === 0) {
    return {
      reply:
        "I am ready to map details. Share age, sex, symptoms, imaging findings, or suspected diagnosis and I will update the case card.",
      patch: {}
    };
  }

  return {
    reply: `Captured ${mappedCount} update${mappedCount === 1 ? "" : "s"} from your message and merged it into the case card.`,
    patch
  };
}

function extractPatchFromText(text: string): Partial<CaseCardDraft> {
  const normalized = text.trim();
  if (!normalized) {
    return {};
  }

  const patch: Partial<CaseCardDraft> = {};

  const ageMatch = normalized.match(/(\d{1,3})\s*[- ]?(?:year|yr)/i);
  if (ageMatch?.[1]) {
    patch.patientAge = ageMatch[1];
  }

  if (/\bmale\b|\bman\b/i.test(normalized)) {
    patch.patientSex = "Male";
  } else if (/\bfemale\b|\bwoman\b/i.test(normalized)) {
    patch.patientSex = "Female";
  }

  const symptoms = symptomDictionary
    .filter(([pattern]) => pattern.test(normalized))
    .map(([, label]) => label);
  if (symptoms.length > 0) {
    patch.currentSymptoms = symptoms;
  }

  if (/ct|computed tomography/i.test(normalized)) {
    patch.imagingModality = "CT Chest";
  } else if (/mri/i.test(normalized)) {
    patch.imagingModality = "MRI";
  } else if (/x[- ]?ray/i.test(normalized)) {
    patch.imagingModality = "X-ray";
  }

  if (/right hilar mass|lung mass|pulmonary mass|mediastinal/i.test(normalized)) {
    patch.suspectedDiagnosis = "Possible lung malignancy";
  } else if (/stroke|mca occlusion|ischemic/i.test(normalized)) {
    patch.suspectedDiagnosis = "Acute ischemic stroke";
  } else if (/pneumonia/i.test(normalized)) {
    patch.suspectedDiagnosis = "Community-acquired pneumonia";
  }

  if (/allerg/i.test(normalized) && /none/i.test(normalized)) {
    patch.allergies = "No known allergies";
  }

  const vitalsMatch = normalized.match(
    /(bp|blood pressure|hr|heart rate|spo2|oxygen saturation)[^.!?\n]{0,50}/i
  );
  if (vitalsMatch?.[0]) {
    patch.vitals = vitalsMatch[0].trim();
  }

  if (normalized.length > 35) {
    patch.clinicalSummary = normalized.slice(0, 220);
  }

  if (!patch.presentingConcern && symptoms.length > 0) {
    patch.presentingConcern = symptoms.slice(0, 2).join(", ");
  } else if (/presenting with|complaint of|reports/i.test(normalized)) {
    patch.presentingConcern = normalized.slice(0, 120);
  }

  return patch;
}

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface RouteCenter {
  name: string;
  url?: string;
  capability: string;
  travel: string;
  reason: string;
  lat: number;
  lng: number;
}

export async function findHospitalsRoute(
  diagnosis: string,
  location?: string,
  equipment?: Record<string, boolean>,
  maxTravelTime?: number,
  maxDistance?: string
): Promise<RouteCenter[]> {
  const formData = new FormData();
  formData.append("diagnosis", diagnosis);
  if (location) {
    formData.append("location", location);
  }

  // Format equipment into a comma separated list of the checked items
  if (equipment) {
    const requiredEq = Object.entries(equipment)
      .filter(([_, isChecked]) => isChecked)
      .map(([name]) => name)
      .join(", ");
    if (requiredEq) {
      formData.append("equipment", requiredEq);
    }
  }

  if (maxTravelTime) {
    formData.append("maxTravelTime", maxTravelTime.toString());
  }

  if (maxDistance) {
    formData.append("maxDistance", maxDistance);
  }

  const response = await fetch(`${API_BASE}/search_hospitals`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let errMessage = `Routing fell back (${response.status})`;
    try {
      const data = await response.json();
      errMessage = data.detail || data.error || JSON.stringify(data);
    } catch {
      errMessage = await response.text() || errMessage;
    }
    console.error(errMessage);
    throw new Error(errMessage);
  }

  const data = await response.json() as any;
  console.log("RAW ROUTE DATA FROM BACKEND:", data);
  let centers = data.centers || data.hospitals || data;

  if (!Array.isArray(centers)) {
    if (centers && Array.isArray(centers.centers)) {
      centers = centers.centers;
    } else if (centers && Array.isArray(centers.hospitals)) {
      centers = centers.hospitals;
    } else if (Array.isArray(data)) {
      centers = data;
    } else {
      console.warn("Could not find array in route response, falling back to empty.");
      centers = [];
    }
  }

  console.log("PARSED CENTERS ARRAY:", centers);
  return centers as RouteCenter[];
}
