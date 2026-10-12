<script setup>
// The validator's findings over the work repo (#374), as `lmer-schema validate
// --json` printed them: one row per finding, in the document's order — which is
// the CLI's fix-list order, most severe first — and every field as written. The
// document is the CLI's own and is never re-spelled here; the only thing this
// adds is a sentence saying what the counts amount to.
//
// The header carries the form and the strictness tier, because the same finding
// means different things at each: a legacy tree is checked at the lenient tier,
// where the list is a migration aid and not a gate. `unusable` is said in the
// header when it is set — nothing below it could be judged, so it is the verdict.
import { computed } from 'vue'

const props = defineProps({
  form: { type: String, default: null },
  tier: { type: String, default: null },
  document: { type: Object, default: null },
})

const findings = computed(() => props.document?.findings || [])

const SEVERITY_COLOR = { ERROR: 'error', WARN: 'warning', INFO: 'info' }

// Only ERROR affects conformance (D14). A list of nothing but INFO is the common
// healthy case — a host bucket, a README index still to be generated — and has to
// read as notes rather than as a wall of problems; an empty list is a clean tree
// and has to say so rather than render an empty card.
const summary = computed(() => {
  if (!props.document) return null
  const counts = props.document.counts || {}
  const errors = counts.error || 0
  const warnings = counts.warn || 0
  const notes = counts.info || 0
  if (!findings.value.length) return 'Nothing to report: the validator found no errors, warnings or notes.'
  if (!errors && !warnings) {
    return `No errors or warnings — ${notes} notes, which are information and block nothing.`
  }
  return `${errors} errors, ${warnings} warnings, ${notes} notes.`
    + (errors ? ' Fix the errors, in order: only they affect conformance.' : '')
})
</script>

<template>
  <div class="validator-findings">
    <div class="section-title">Validator findings</div>
    <v-card class="mb-3">
      <v-card-text>
        <div class="findings-header d-flex flex-wrap align-center ga-3 text-body-small mb-2">
          <span>form {{ form || 'unknown' }}</span>
          <span>tier {{ tier || 'unknown' }}</span>
          <span v-if="document?.unusable" class="text-error">
            unusable: {{ document.unusable }}
          </span>
        </div>

        <p v-if="!document" class="text-medium-emphasis">
          This tree was not validated: only a work repo in schema form is.
        </p>

        <template v-else>
          <p class="text-medium-emphasis mb-2">{{ summary }}</p>
          <div
            v-for="(finding, index) in findings"
            :key="index"
            class="finding-row py-2"
          >
            <div class="d-flex flex-wrap align-center ga-2">
              <v-chip
                class="finding-severity"
                size="small"
                variant="tonal"
                :color="SEVERITY_COLOR[finding.severity]"
              >{{ finding.severity }}</v-chip>
              <code class="finding-code">{{ finding.code }}</code>
              <code class="finding-path scroll-x text-medium-emphasis">{{ finding.path }}</code>
            </div>
            <div class="finding-message text-body-small mt-1">{{ finding.message }}</div>
            <div
              v-if="finding.detail"
              class="finding-detail text-body-small text-medium-emphasis mt-1"
            >{{ finding.detail }}</div>
          </div>
        </template>
      </v-card-text>
    </v-card>
  </div>
</template>
