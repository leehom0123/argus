// Studies (Optuna multirun) — v0.2 hyperopt-ui.
//
// Sibyl's monitor.py callback labels each Hydra-multirun job with
// optuna.study_name + optuna.trial_id + optuna.params; the backend
// stashes this on Job.extra and exposes the three endpoints below.
// All routes are visibility-filtered server-side, so the FE doesn't
// have to re-implement RBAC.

import { http } from './client';

export interface StudySummary {
  study_name: string;
  n_trials: number;
  n_done: number;
  n_failed: number;
  best_value: number | null;
  best_metric: string | null;
  direction: string | null;
  sampler: string | null;
  last_run: string | null;
}

export interface StudyListOut {
  studies: StudySummary[];
}

export interface TrialRow {
  trial_id: number;
  job_id: string;
  batch_id: string;
  status: string | null;
  start_time: string | null;
  end_time: string | null;
  elapsed_s: number | null;
  params: Record<string, unknown>;
  value: number | null;
  metric_name: string | null;
  metrics: Record<string, unknown> | null;
}

export interface StudyDetailOut {
  study_name: string;
  direction: string | null;
  sampler: string | null;
  n_trials: number;
  n_done: number;
  n_failed: number;
  best_value: number | null;
  best_metric: string | null;
  param_keys: string[];
  metric_keys: string[];
  trials: TrialRow[];
}

export interface TrialDetailOut {
  study_name: string;
  trial_id: number;
  job_id: string;
  batch_id: string;
  status: string | null;
  start_time: string | null;
  end_time: string | null;
  elapsed_s: number | null;
  params: Record<string, unknown>;
  metrics: Record<string, unknown> | null;
  value: number | null;
  metric_name: string | null;
}

export type StudySortKey = 'value' | 'trial_id' | 'start_time';
export type StudySortOrder = 'asc' | 'desc';

export async function listStudies(): Promise<StudyListOut> {
  const { data } = await http.get<StudyListOut>('/studies');
  return data;
}

export async function getStudy(
  studyName: string,
  params?: { sort?: StudySortKey; order?: StudySortOrder },
): Promise<StudyDetailOut> {
  const { data } = await http.get<StudyDetailOut>(
    `/studies/${encodeURIComponent(studyName)}`,
    { params },
  );
  return data;
}

export async function getTrial(
  studyName: string,
  trialId: number,
): Promise<TrialDetailOut> {
  const { data } = await http.get<TrialDetailOut>(
    `/studies/${encodeURIComponent(studyName)}/trials/${trialId}`,
  );
  return data;
}
