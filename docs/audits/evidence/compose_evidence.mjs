// Read-only evidence: what the composer produces per preferredLanguage.
import { composeBilingualEmail } from '/home/msi/Desktop/Development/skydiving/skydiving-os/backend/src/utils/bilingualEmail.js';

const render = (lang) => (lang === 'fr'
  ? { subject: 'Votre vidéo de saut est prête', text: 'Bonjour Alex,\nVotre montage est terminé.', html: '<p>Bonjour Alex,</p>' }
  : { subject: 'Your skydive video is ready', text: 'Hi Alex,\nYour edit is finished.', html: '<p>Hi Alex,</p>' });

for (const pref of ['fr', 'en', null, 'de', 'fr-CA', 'EN']) {
  const c = composeBilingualEmail(render, { primaryLang: pref });
  console.log(`preferredLanguage=${JSON.stringify(pref)} EMAIL_PRIMARY_LANGUAGE=${process.env.EMAIL_PRIMARY_LANGUAGE ?? '(unset)'}`);
  console.log(`  order   : ${c.order.join(' -> ')}`);
  console.log(`  subject : ${c.subject}`);
  console.log(`  text[0..3]: ${JSON.stringify(c.text.split('\n').slice(0, 3))}`);
  console.log(`  html marker: ${c.html.split('\n')[0]}`);
}
