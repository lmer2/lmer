<script setup>
// Ratchet's face (issue 581): one mood in, one <img> out. The fade and the
// off treatment live here so the pane header and the bar button agree. The
// files are chadd's 128px art, downscaled in CSS only.
import { computed } from 'vue'
import { assistantName, faceFile } from '../ratchet.js'

const props = defineProps({
  mood: { type: String, required: true },
  off: { type: Boolean, default: false },
  size: { type: Number, default: 24 },
})
const file = computed(() => (props.off ? faceFile('off') : faceFile(props.mood)))
const alt = computed(() => `${assistantName.value}, ${props.off ? 'off' : props.mood}`)
</script>

<template>
  <span class="ratchet" :class="{ 'ratchet-off': off }" :style="{ width: `${size}px`, height: `${size}px` }">
    <Transition name="ratchet-fade" mode="out-in">
      <img :key="file" class="ratchet-face" :src="`./assets/ratchet/${file}.png`"
        :alt="alt" :width="size" :height="size" decoding="async" />
    </Transition>
  </span>
</template>

<style scoped>
.ratchet { position: relative; display: inline-block; line-height: 0; }
.ratchet-face { display: block; transition: opacity 150ms ease; }
.ratchet-fade-enter-from, .ratchet-fade-leave-to { opacity: 0; }
/* Off: desaturated, dimmed, and a diagonal slash in the text colour — no hex,
   the theme rule (tests/test_platform_web_theme.py). */
.ratchet-off .ratchet-face { filter: grayscale(1); opacity: 0.45; }
.ratchet-off::after {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(135deg, transparent 46%, currentColor 46%, currentColor 54%, transparent 54%);
  pointer-events: none;
}
@media (prefers-reduced-motion: reduce) {
  .ratchet-face { transition: none; }
}
</style>
