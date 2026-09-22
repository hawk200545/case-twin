import type { CaseProfile } from "./caseProfileTypes";
import { emptyProfile } from "./caseProfileTypes";
import { API_BASE } from "./api";
// ─── Confidence scoring ────────────────────────────────────────────────────

interface ConfidenceField {
    label: string;
    filled: (p: CaseProfile) => boolean;
}

const CONFIDENCE_FIELDS: ConfidenceField[] = [
    { label: "Patient age", filled: (p) => p.patient.age_years != null },
    { label: "Patient sex", filled: (p) => Boolean(p.patient.sex) },
    { label: "Comorbidities", filled: (p) => p.patient.comorbidities.length > 0 },
    { label: "Chief complaint", filled: (p) => Boolean(p.presentation.chief_complaint) },
    { label: "HPI", filled: (p) => Boolean(p.presentation.hpi) },
    { label: "PMH", filled: (p) => Boolean(p.presentation.pmh) },
    { label: "Imaging modality", filled: (p) => Boolean(p.study.modality) },
    { label: "Body region", filled: (p) => Boolean(p.study.body_region) },
    { label: "Image available", filled: (p) => Boolean(p.study.image_url) },
    { label: "Primary diagnosis", filled: (p) => Boolean(p.assessment.diagnosis_primary) },
    { label: "Urgency", filled: (p) => Boolean(p.assessment.urgency) },
    { label: "Summary one-liner", filled: (p) => Boolean(p.summary.one_liner) },
    { label: "Key points", filled: (p) => p.summary.key_points.length > 0 },
];

export function computeProfileConfidence(profile: CaseProfile): {
    score: number;
    filled: number;
    total: number;
    missing: string[];
} {
    const total = CONFIDENCE_FIELDS.length;
    const filledFields = CONFIDENCE_FIELDS.filter((f) => f.filled(profile));
    const missingFields = CONFIDENCE_FIELDS.filter((f) => !f.filled(profile));

    return {
        score: Math.round((filledFields.length / total) * 100),
        filled: filledFields.length,
        total,
        missing: missingFields.map((f) => f.label),
    };
}

// ─── Extraction ────────────────────────────────────────────────────────────

export async function extractCaseProfile(
    images: File[],
    notes: string,
    notesFile: File | null
): Promise<CaseProfile> {
    let extractionError: string | null = null;
    // Try backend first
    try {
        const form = new FormData();
        images.forEach((img) => form.append("images", img));
        if (notesFile) form.append("notes_file", notesFile);
        form.append("notes", notes);

        const res = await fetch(`${API_BASE}/extract`, {
            method: "POST",
            body: form,
        });

        if (!res.ok) {
            const payload = await res.json().catch(() => null) as { detail?: string } | null;
            throw new Error(payload?.detail || `Extraction failed (${res.status})`);
        }
        const data = await res.json() as { profile: CaseProfile };
        return data.profile;
    } catch (error) {
        // backend offline — fall through to client-side mock
        extractionError = error instanceof Error ? error.message : "Document extraction could not be completed.";
    }

    // Client-side mock extraction
    await delay(1200);
    const fallback = clientSideExtract(images, notes);
    // Do not silently replace a rejected PDF with binary garbage or an empty
    // profile. The profile view surfaces this actionable import message.
    if (notesFile && extractionError) {
        fallback.extra_fields.document_import = extractionError;
    }
    return fallback;
}

// ─── Client-side mock extraction (regex-based) ─────────────────────────────

function clientSideExtract(images: File[], notes: string): CaseProfile {
    const p = emptyProfile();
    const id = crypto.randomUUID();
    p.profile_id = `${id}:${crypto.randomUUID()}`;
    p.case_id = id;
    p.image_id = crypto.randomUUID();

    const text = notes.trim();

    // --- patient ---
    const ageMatch = text.match(/(\d{1,3})\s*[- ]?(?:year|yr)s?[- ]?old/i);
    if (ageMatch?.[1]) p.patient.age_years = parseInt(ageMatch[1], 10);

    if (/\bfemale\b|\bwoman\b|\bF\b/i.test(text)) p.patient.sex = "female";
    else if (/\bmale\b|\bman\b|\bM\b/i.test(text)) p.patient.sex = "male";

    if (/immunocompromised|immunosuppressed/i.test(text))
        p.patient.immunocompromised = "yes";
    else if (text.length > 20) p.patient.immunocompromised = "no";

    const comorbidityPatterns: [RegExp, string][] = [
        [/hypertension|HTN/i, "hypertension"],
        [/type 2 diabet|T2DM|DM2/i, "type 2 diabetes"],
        [/type 1 diabet|T1DM|DM1/i, "type 1 diabetes"],
        [/atrial fibrillation|AF\b|AFib/i, "atrial fibrillation"],
        [/heart failure|CHF/i, "heart failure"],
        [/COPD|chronic obstructive/i, "COPD"],
        [/asthma/i, "asthma"],
        [/cirrhosis|liver cirrhosis/i, "liver cirrhosis"],
        [/hepatocellular carcinoma|HCC/i, "hepatocellular carcinoma"],
        [/chronic kidney|CKD/i, "chronic kidney disease"],
        [/coronary artery disease|CAD/i, "coronary artery disease"],
        [/obesity/i, "obesity"],
        [/malignancy|cancer/i, "malignancy"],
    ];
    p.patient.comorbidities = comorbidityPatterns
        .filter(([re]) => re.test(text))
        .map(([, label]) => label);

    if (/no known allerg/i.test(text)) p.patient.allergies = "no known allergies";

    // --- presentation ---
    const chiefComplaintMatch = text.match(
        /(?:present(?:ing)? with|complaint of|admitted for|scheduled for)\s+([^.!?\n]{5,100})/i
    );
    if (chiefComplaintMatch?.[1])
        p.presentation.chief_complaint = chiefComplaintMatch[1].trim();

    const durationMatch = text.match(
        /(?:for|over|duration of)\s+((?:\d+\s*)?(?:day|week|month|year)s?)/i
    );
    if (durationMatch?.[1]) p.presentation.symptom_duration = durationMatch[1].trim();

    if (text.length > 40) p.presentation.hpi = text.slice(0, 500);

    const pmhPatterns: string[] = [];
    comorbidityPatterns.forEach(([re, label]) => {
        if (re.test(text)) pmhPatterns.push(label);
    });
    if (pmhPatterns.length > 0) p.presentation.pmh = pmhPatterns.join(", ");

    // --- study ---
    if (images.length > 0) {
        const firstName = images[0].name.toLowerCase();
        if (/ct|computed tomography/i.test(text + firstName)) {
            p.study.modality = "CT";
            p.study.image_type = "radiology";
            p.study.image_subtype = "ct";
        } else if (/mri/i.test(text + firstName)) {
            p.study.modality = "MRI";
            p.study.image_type = "radiology";
            p.study.image_subtype = "mri";
        } else if (/x[- ]?ray|cxr|chest x/i.test(text + firstName)) {
            p.study.modality = "CXR";
            p.study.image_type = "radiology";
            p.study.image_subtype = "x_ray";
        } else {
            p.study.modality = "Imaging";
            p.study.image_type = "radiology";
        }

        if (/thorax|chest|pulmonary|lung/i.test(text)) {
            p.study.body_region = "thorax";
            p.study.radiology_region = "thorax";
        } else if (/abdomen|abdominal|liver/i.test(text)) {
            p.study.body_region = "abdomen";
        } else if (/brain|head|neuro/i.test(text)) {
            p.study.body_region = "head";
        }

        if (/PA|posteroanterior/i.test(text)) p.study.view_position = "PA";
        else if (/AP|anteroposterior/i.test(text)) p.study.view_position = "AP";
        else if (/lateral/i.test(text)) p.study.view_position = "lateral";

        // Attach local object URL for the first image thumbnail
        p.study.image_url = URL.createObjectURL(images[0]);
    }

    // --- assessment ---
    const diagMap: [RegExp, string][] = [
        [/scimitar/i, "scimitar syndrome"],
        [/pneumonia/i, "community-acquired pneumonia"],
        [/pulmonary embolism|PE\b/i, "pulmonary embolism"],
        [/lung malignancy|lung cancer|NSCLC|SCLC/i, "lung malignancy"],
        [/stroke|ischemic/i, "acute ischemic stroke"],
        [/heart failure|pulmonary edema/i, "heart failure"],
        [/pneumothorax/i, "pneumothorax"],
        [/pleural effusion/i, "pleural effusion"],
        [/aortic dissection/i, "aortic dissection"],
    ];

    for (const [re, diag] of diagMap) {
        if (re.test(text)) {
            p.assessment.diagnosis_primary = diag;
            p.assessment.suspected_primary = [diag, ...p.patient.comorbidities.slice(0, 2)];
            break;
        }
    }

    if (/urgent|emergency|stat/i.test(text)) p.assessment.urgency = "emergent";
    else if (/routine|elective|scheduled/i.test(text)) p.assessment.urgency = "routine";
    else if (text.length > 20) p.assessment.urgency = "semi-urgent";

    if (/infection|sepsis|pneumonia|fever/i.test(text))
        p.assessment.infectious_concern = "yes";
    else if (text.length > 20) p.assessment.infectious_concern = "no";

    if (/icu|intensive care|critical/i.test(text)) p.assessment.icu_candidate = "yes";

    // --- summary ---
    if (p.patient.age_years && p.patient.sex && p.assessment.diagnosis_primary) {
        const meds = p.patient.comorbidities.slice(0, 3).join(", ");
        p.summary.one_liner = `${p.patient.age_years}-year-old ${p.patient.sex} with ${meds || "multiple comorbidities"} presenting with ${p.presentation.chief_complaint ?? p.assessment.diagnosis_primary}.`;
    }

    if (p.assessment.diagnosis_primary)
        p.summary.key_points = [`Primary finding: ${p.assessment.diagnosis_primary}`];

    // --- extra_fields (schema expansion) ---
    // Capture at any confidence level — anything outside the base schema.
    const extra: Record<string, string | string[]> = {};

    // Smoking
    if (/non[- ]?smok|never smoked|no smoking/i.test(text)) {
        extra.smoking_status = "non-smoker";
    } else {
        const smokeM = text.match(/(?:smok(?:ing|er|es)|tobacco)[^\.\n]{0,60}?((?:\d+\s*)?(?:pack[- ]?year|cigarette|cigar|pipe)[^\.\n]{0,40})?/i);
        if (smokeM) extra.smoking_status = (smokeM[1] ?? "").trim() || "smoker";
    }

    // Alcohol
    const alcoholM = text.match(/alcohol[^\.\n]{0,80}/i);
    if (alcoholM) extra.alcohol_use = alcoholM[0].trim().slice(0, 120);

    // BMI
    const bmiM = text.match(/BMI\s*(?:of\s*)?(\d{1,2}(?:\.\d)?)/i);
    if (bmiM) extra.bmi = bmiM[1];

    // Blood type
    const bloodM = text.match(/\b(A|B|AB|O)[+-]?\s*blood\s*type|\bblood\s*type\s*(A|B|AB|O)[+-]?\b/i);
    if (bloodM) extra.blood_type = (bloodM[1] || bloodM[2])!.toUpperCase();

    // Family history
    const famM = text.match(/family\s*(?:history|hx)[^\.\n]{0,150}/i);
    if (famM) extra.family_history = famM[0].trim().slice(0, 200);

    // Occupation
    const occM = text.match(/(?:occupation|works?\s*as|employed\s*(?:as|at)|profession)[^\.\n]{0,80}/i);
    if (occM) extra.occupation = occM[0].trim().slice(0, 120);

    // Ethnicity
    const ethM = text.match(/(?:ethnicity|race|racial background)\s*[:\-]?\s*([A-Za-z\s\-]+)/i);
    if (ethM) extra.ethnicity = ethM[1].trim().slice(0, 60);

    // Vaccination
    const vaxM = text.match(/(?:vaccin|immuniz)[^\.\n]{0,80}/i);
    if (vaxM) extra.vaccination = vaxM[0].trim().slice(0, 120);

    // Travel
    const travelM = text.match(/(?:travel(?:led|ed)?\s*(?:to|from)|recent\s*travel)[^\.\n]{0,100}/i);
    if (travelM) extra.travel_history = travelM[0].trim().slice(0, 150);

    // Functional status
    const funcM = text.match(/(?:functional status|ADLs?|activities of daily|ambulates?|independent)[^\.\n]{0,80}/i);
    if (funcM) extra.functional_status = funcM[0].trim().slice(0, 120);

    // Code status
    const codeM = text.match(/(?:code\s*status|full\s*code|DNR|DNI|comfort\s*care)[^\.\n]{0,60}/i);
    if (codeM) extra.code_status = codeM[0].trim().slice(0, 80);

    // Social history
    const socialM = text.match(/social\s*(?:history|hx)[^\.\n]{0,200}/i);
    if (socialM) extra.social_history = socialM[0].trim().slice(0, 250);

    p.extra_fields = extra;

    return p;
}

function delay(ms: number) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
