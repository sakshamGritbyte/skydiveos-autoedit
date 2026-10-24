import nodemailer from '/home/msi/Desktop/Development/skydiving/skydiving-os/backend/node_modules/nodemailer/lib/nodemailer.js';
const t = nodemailer.createTransport({ streamTransport: true, buffer: true, newline: 'unix' });
const info = await t.sendMail({ from: 'a@b.c', to: 'x@y.z', subject: 'Votre vidéo est prête / Your video is ready', text: 'Bonjour — été', html: '<p>Bonjour — été</p>' });
const raw = info.message.toString();
console.log(raw.split('\n').filter(l => /^(Subject|Content-Type|Content-Transfer-Encoding):/i.test(l)).join('\n'));
