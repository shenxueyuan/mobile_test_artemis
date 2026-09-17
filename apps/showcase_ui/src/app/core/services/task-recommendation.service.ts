/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, finalize, tap } from 'rxjs';
import {
  AppReference,
  SmartSuggestion,
  SmartTaskCatalog,
  SuggestionCategory
} from '../data/smart-tasks.data';

/** Payload accepted by the preset CRUD endpoints. */
export interface SmartTaskPayload {
  id?: string;
  title: string;
  description?: string;
  goal: string;
  profile: string;
  category: string;
  tag?: string;
  requiredPackages?: string[];
  matchMode?: string;
  priority?: number;
  apps?: AppReference[];
}

@Injectable({
  providedIn: 'root'
})
export class TaskRecommendationService {
  private http = inject(HttpClient);

  /** Presets loaded from the backend, editable through the console. */
  public readonly tasks = signal<SmartSuggestion[]>([]);
  /** Package -> display metadata, used to render app badges and to derive them. */
  public readonly appRegistry = signal<Record<string, AppReference>>({});
  /** Where the presets are stored, shown in the management panel. */
  public readonly editablePath = signal<string>('');
  public readonly isLoading = signal<boolean>(false);
  public readonly isSaving = signal<boolean>(false);
  public readonly loadError = signal<string | null>(null);

  public readonly allTasks = computed(() => this.tasks());

  /** Load the catalog from the backend. */
  public load(): Observable<SmartTaskCatalog> {
    this.isLoading.set(true);
    this.loadError.set(null);
    return this.http.get<SmartTaskCatalog>('/api/smart-tasks').pipe(
      tap({
        next: (catalog) => {
          this.tasks.set(catalog.tasks ?? []);
          this.appRegistry.set(catalog.apps ?? {});
          this.editablePath.set(catalog.editablePath ?? '');
        },
        error: (err) => this.loadError.set(this.describeError(err, '加载推荐任务失败。'))
      }),
      finalize(() => this.isLoading.set(false))
    );
  }

  /** Create a preset, then refresh the catalog. */
  public create(payload: SmartTaskPayload): Observable<SmartSuggestion> {
    return this.mutate(
      this.http.post<SmartSuggestion>('/api/smart-tasks', payload),
      '新增推荐任务失败。'
    );
  }

  /** Replace a preset, then refresh the catalog. */
  public update(id: string, payload: SmartTaskPayload): Observable<SmartSuggestion> {
    return this.mutate(
      this.http.put<SmartSuggestion>(`/api/smart-tasks/${encodeURIComponent(id)}`, payload),
      '保存推荐任务失败。'
    );
  }

  /** Delete a preset, then refresh the catalog. */
  public remove(id: string): Observable<unknown> {
    return this.mutate(
      this.http.delete(`/api/smart-tasks/${encodeURIComponent(id)}`),
      '删除推荐任务失败。'
    );
  }

  /** Restore the shipped catalog, then refresh. */
  public reset(): Observable<{ tasks: SmartSuggestion[] }> {
    return this.mutate(
      this.http.post<{ tasks: SmartSuggestion[] }>('/api/smart-tasks/reset', {}),
      '恢复默认推荐任务失败。'
    );
  }

  /**
   * Check if task matches any package installed on device
   */
  public isSuggestionOnDevice(
    suggestion: SmartSuggestion,
    installedPackages: Set<string>
  ): boolean {
    if (installedPackages.size === 0) return false;
    if (!suggestion.requiredPackages || suggestion.requiredPackages.length === 0) {
      return true;
    }

    if (suggestion.matchMode === 'all') {
      return suggestion.requiredPackages.every(pkg => installedPackages.has(pkg));
    }
    return suggestion.requiredPackages.some(pkg => installedPackages.has(pkg));
  }

  /**
   * Intelligently rank and filter tasks:
   * Prioritizes tasks matching apps detected on user's phone without exposing clutter.
   */
  public filterAndRankTasks(
    installedPackages: Set<string>,
    category: SuggestionCategory,
    shuffleOffset: number = 0
  ): SmartSuggestion[] {
    const hasDevice = installedPackages.size > 0;

    // 1. Score tasks: prioritize tasks whose apps are on the user's phone
    const scored = this.tasks().map(task => {
      const isMatched = this.isSuggestionOnDevice(task, installedPackages);
      let score = task.priority ?? 50;

      if (hasDevice) {
        if (isMatched) {
          score += 100;
          if (task.requiredPackages && task.requiredPackages.length > 1 && task.matchMode === 'all') {
            score += 30; // bonus for full multi-app synergy match
          }
        } else {
          score -= 40;
        }
      }

      return { task, isMatched, score };
    });

    // 2. Filter by category
    const filtered = scored.filter(item => {
      if (category === 'flash') {
        return item.task.profile === 'flash';
      }
      if (category === 'pro') {
        return item.task.profile === 'pro';
      }
      if (category === 'cross_app') {
        return item.task.category === 'cross_app';
      }
      if (category === 'monitor') {
        return item.task.category === 'monitor';
      }
      return true; // 'all'
    });

    // 3. Sort by score descending (matched apps first)
    filtered.sort((a, b) => b.score - a.score);

    let result = filtered.map(f => f.task);

    // 4. Apply shuffle rotation
    if (shuffleOffset > 0 && result.length > 0) {
      const shift = shuffleOffset % result.length;
      result = [...result.slice(shift), ...result.slice(0, shift)];
    }

    return result;
  }

  /** Resolve display metadata for the packages a preset depends on. */
  public buildApps(packages: string[]): AppReference[] {
    const registry = this.appRegistry();
    return packages.map(pkg => registry[pkg] ?? { name: pkg, icon: 'apps', pkg });
  }

  /** Run a mutation and reload the catalog so every view stays in sync. */
  private mutate<T>(request: Observable<T>, fallbackMessage: string): Observable<T> {
    this.isSaving.set(true);
    this.loadError.set(null);
    return request.pipe(
      tap({
        next: () => this.load().subscribe(),
        error: (err) => this.loadError.set(this.describeError(err, fallbackMessage))
      }),
      finalize(() => this.isSaving.set(false))
    );
  }

  private describeError(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    const message = (err as { message?: string })?.message;
    return detail || message || fallback;
  }
}
