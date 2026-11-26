// The Architecture: browser visualization.
// Renders the supplied 5-DOF lamp on a pyramid, drives it from /ws/viz state
// frames, and shows the character HUD.

import * as THREE from 'three';
import URDFLoader from 'urdf-loader';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';

// ---------------------------------------------------------------- constants
const URDF_URL = '/robot/dummy_lamp_5dof.urdf';
const JOINTS = ['base_yaw_joint', 'shoulder_pitch_joint', 'elbow_pitch_joint', 'neck_yaw_joint', 'head_pitch_joint'];
const REST_POSE = { base_yaw_joint: 0, shoulder_pitch_joint: 0.35, elbow_pitch_joint: -1.0, neck_yaw_joint: 0, head_pitch_joint: -0.2 };

const PYRAMID_H = 1.2;     // m
const TOP_HALF = 0.25;     // platform is 0.5 m wide
const BASE_HALF = 1.19;    // ~52 degree faces
const BEAM_LEN = 2.6;      // visible beam length, m
const BEAM_MIN = THREE.MathUtils.degToRad(12);  // full cone angle at beam = 0
const BEAM_MAX = THREE.MathUtils.degToRad(40);  // full cone angle at beam = 1
const SPOT_CANDELA = 55;   // spotlight intensity at light.intensity = 1

const CAM_HOME = new THREE.Vector3(1.02, PYRAMID_H + 0.84, 1.78);
const CAM_TARGET = new THREE.Vector3(0, PYRAMID_H + 0.12, 0);

const GOLD = new THREE.Color('#c9a45c');

const qs = new URLSearchParams(location.search);

// ------------------------------------------------------------------ renderer
const stage = document.getElementById('stage');
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight, false);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color('#030404');
scene.fog = new THREE.FogExp2('#0a100d', 0.16);

const camera = new THREE.PerspectiveCamera(38, window.innerWidth / window.innerHeight, 0.02, 60);
camera.position.copy(CAM_HOME);
// ?view=front|side|top for checking orientation
const VIEWS = {
  front: new THREE.Vector3(0, PYRAMID_H + 0.45, 2.3),
  side: new THREE.Vector3(2.3, PYRAMID_H + 0.45, 0),
  top: new THREE.Vector3(0.01, PYRAMID_H + 2.6, 0.01),
};
if (VIEWS[qs.get('view')]) camera.position.copy(VIEWS[qs.get('view')]);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.copy(CAM_TARGET);
controls.enableDamping = true;
controls.dampingFactor = 0.05;
controls.enablePan = false;
controls.rotateSpeed = 0.45;
controls.zoomSpeed = 0.6;
controls.minDistance = 0.9;
controls.maxDistance = 4.2;
controls.minPolarAngle = 0.55;
controls.maxPolarAngle = 1.62;
controls.minAzimuthAngle = -1.4;
controls.maxAzimuthAngle = 1.4;
controls.update();

// Dim, neutral environment so chrome has something to reflect.
const pmrem = new THREE.PMREMGenerator(renderer);
const envMap = pmrem.fromScene(new RoomEnvironment(renderer), 0.04).texture;

// --------------------------------------------------------------- base light
scene.add(new THREE.HemisphereLight('#2a2a22', '#06140c', 0.35));

const rim = new THREE.DirectionalLight('#d8c08a', 0.55);   // gold rim from behind
rim.position.set(-1.2, 3.2, -2.6);
scene.add(rim);

const fill = new THREE.DirectionalLight('#3d7a5a', 0.18);  // faint green from the side
fill.position.set(2.5, 1.0, 1.5);
scene.add(fill);

// ---------------------------------------------------------------- backdrop
{
  // A large inside-out sphere: black above, faint deep green at the horizon.
  const geo = new THREE.SphereGeometry(30, 48, 24);
  const mat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    fog: false,
    uniforms: { uFog: { value: scene.fog.color } },
    vertexShader: `varying vec3 vP; void main(){ vP = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.); }`,
    fragmentShader: `uniform vec3 uFog; varying vec3 vP;
      void main(){
        float h = vP.y;
        vec3 top = vec3(0.0006, 0.0008, 0.0008);
        vec3 glow = uFog * 1.6 + vec3(0.0, 0.0025, 0.0012);
        vec3 c = mix(top, glow, exp(-max(h, 0.0) * 7.0));
        c = mix(c, uFog, smoothstep(0.03, -0.01, h));
        gl_FragColor = vec4(c, 1.0);
      }`,
  });
  scene.add(new THREE.Mesh(geo, mat));
}

// ------------------------------------------------------------------- floor
function makeGridTexture() {
  const s = 1024;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d');
  g.clearRect(0, 0, s, s);
  const cell = s / 32;
  g.strokeStyle = 'rgba(90, 190, 130, 1)';
  g.lineWidth = 1.2;
  for (let i = 0; i <= 32; i++) {
    g.beginPath(); g.moveTo(i * cell, 0); g.lineTo(i * cell, s); g.stroke();
    g.beginPath(); g.moveTo(0, i * cell); g.lineTo(s, i * cell); g.stroke();
  }
  // radial fade
  g.globalCompositeOperation = 'destination-in';
  const rg = g.createRadialGradient(s / 2, s / 2, s * 0.08, s / 2, s / 2, s / 2);
  rg.addColorStop(0, 'rgba(0,0,0,1)');
  rg.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = rg;
  g.fillRect(0, 0, s, s);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}
{
  const floor = new THREE.Mesh(
    new THREE.CircleGeometry(24, 64),
    new THREE.MeshStandardMaterial({ color: '#0a0c0b', roughness: 0.82, metalness: 0.15 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);

  const grid = new THREE.Mesh(
    new THREE.PlaneGeometry(16, 16),
    new THREE.MeshBasicMaterial({ map: makeGridTexture(), transparent: true, opacity: 0.07, blending: THREE.AdditiveBlending, depthWrite: false }),
  );
  grid.rotation.x = -Math.PI / 2;
  grid.position.y = 0.002;
  scene.add(grid);
}

// ----------------------------------------------------------------- pyramid
const pyramid = new THREE.Group();
scene.add(pyramid);
let glyphMat = null;
{
  const a = TOP_HALF, b = BASE_HALF, h = PYRAMID_H;
  const T = [[-a, h, -a], [a, h, -a], [a, h, a], [-a, h, a]];
  const B = [[-b, 0, -b], [b, 0, -b], [b, 0, b], [-b, 0, b]];
  const tris = [];
  const quad = (p, q, r, s) => tris.push(p, q, r, p, r, s);
  quad(B[3], B[2], T[2], T[3]); // front (+z)
  quad(B[2], B[1], T[1], T[2]); // right (+x)
  quad(B[1], B[0], T[0], T[1]); // back
  quad(B[0], B[3], T[3], T[0]); // left
  quad(T[3], T[2], T[1], T[0]); // top
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(tris.flat(), 3));
  geo.computeVertexNormals();

  const stone = new THREE.MeshStandardMaterial({
    color: '#0d0f0e', roughness: 0.32, metalness: 0.55, envMap, envMapIntensity: 0.35, flatShading: true,
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 2,   // keep gold edges from z-fighting
  });
  const body = new THREE.Mesh(geo, stone);
  body.receiveShadow = true;
  body.castShadow = false;
  pyramid.add(body);

  // gold edges
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geo, 10),
    new THREE.LineBasicMaterial({ color: GOLD, transparent: true, opacity: 0.85 }),
  );
  pyramid.add(edges);

  // faint gold courses (horizontal bands) on the faces
  const courseMat = new THREE.LineBasicMaterial({ color: GOLD, transparent: true, opacity: 0.16 });
  for (let i = 1; i < 6; i++) {
    const y = (h * i) / 6;
    const w = b - (b - a) * (y / h) + 0.001;
    const pts = [[-w, y, -w], [w, y, -w], [w, y, w], [-w, y, w], [-w, y, -w]].map((p) => new THREE.Vector3(...p));
    pyramid.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), courseMat));
  }

  // eye-of-providence glyph on the front face
  const tex = makeGlyphTexture();
  glyphMat = new THREE.MeshBasicMaterial({
    map: tex, transparent: true, opacity: 0.32, blending: THREE.AdditiveBlending, depthWrite: false, fog: true,
  });
  const tilt = Math.atan((b - a) / h);
  const yC = 0.58;
  const glyph = new THREE.Mesh(new THREE.PlaneGeometry(0.62, 0.62 / Math.cos(tilt) * 0.86), glyphMat);
  glyph.position.set(0, yC, b - (b - a) * (yC / h) + 0.004);
  glyph.rotation.x = -tilt;
  pyramid.add(glyph);
}

function makeGlyphTexture() {
  const s = 512;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d');
  g.strokeStyle = '#e7c983';
  g.fillStyle = '#e7c983';
  g.lineJoin = 'round';
  g.shadowColor = '#e7c983';
  g.shadowBlur = 14;
  // triangle
  g.lineWidth = 7;
  g.beginPath();
  g.moveTo(s / 2, 40); g.lineTo(s - 40, s - 50); g.lineTo(40, s - 50); g.closePath();
  g.stroke();
  // eye
  const cx = s / 2, cy = s * 0.62, w = 120, hh = 52;
  g.lineWidth = 6;
  g.beginPath();
  g.moveTo(cx - w, cy);
  g.quadraticCurveTo(cx, cy - hh * 2, cx + w, cy);
  g.quadraticCurveTo(cx, cy + hh * 2, cx - w, cy);
  g.stroke();
  g.lineWidth = 5;
  g.beginPath(); g.arc(cx, cy, 34, 0, Math.PI * 2); g.stroke();
  g.beginPath(); g.arc(cx, cy, 13, 0, Math.PI * 2); g.fill();
  // rays
  g.lineWidth = 2.5;
  g.globalAlpha = 0.55;
  for (let i = 0; i < 9; i++) {
    const ang = -Math.PI / 2 + (i - 4) * 0.16;
    const r0 = 150, r1 = 205 - Math.abs(i - 4) * 10;
    g.beginPath();
    g.moveTo(cx + Math.cos(ang) * r0 * 0.35, cy - 150 + Math.sin(ang) * r0 * 0.15 + 30);
    g.lineTo(cx + Math.cos(ang) * r1 * 0.5, cy - 150 + Math.sin(ang) * r1 * 0.45 + 30);
    g.stroke();
  }
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}

// -------------------------------------------------------------------- lamp
// URDF is z-up with the lamp's front along -X. The outer group turns the
// front toward three's +Z (toward the default camera); the inner group
// converts z-up to y-up.
const lampYaw = new THREE.Group();
lampYaw.position.set(0, PYRAMID_H, 0);
lampYaw.rotation.y = Math.PI / 2;
scene.add(lampYaw);

const lampRoot = new THREE.Group();
lampRoot.rotation.x = -Math.PI / 2;
lampYaw.add(lampRoot);

let robot = null;
const lampFx = {};   // spotlight, beam, bulb etc. once the robot is loaded

const matWhite = new THREE.MeshStandardMaterial({ color: '#9d9a93', roughness: 0.42, metalness: 0.05, envMap, envMapIntensity: 0.5 });
const matChrome = new THREE.MeshStandardMaterial({ color: '#7d8187', roughness: 0.22, metalness: 0.92, envMap, envMapIntensity: 0.9 });
const matShade = new THREE.MeshStandardMaterial({ color: '#a8a49b', roughness: 0.38, metalness: 0.05, envMap, envMapIntensity: 0.5, side: THREE.DoubleSide });
const matBlack = new THREE.MeshStandardMaterial({ color: '#0c0d0e', roughness: 0.4, metalness: 0.3 });
const matBulb = new THREE.MeshStandardMaterial({ color: '#000000', emissive: new THREE.Color('#fff2c8'), emissiveIntensity: 2.5, roughness: 0.3 });
const matLens = new THREE.MeshStandardMaterial({ color: '#2a2620', emissive: new THREE.Color('#ffe9b8'), emissiveIntensity: 0.8, roughness: 0.6, side: THREE.DoubleSide });

function linkOf(obj) {
  let o = obj;
  while (o && !o.isURDFLink) o = o.parent;
  return o ? o.name : '';
}

function isVisual(obj) {
  for (let o = obj; o; o = o.parent) {
    if (o.isURDFVisual) return true;
    if (o.isURDFLink) return false;
  }
  return false;
}

function styleRobot() {
  robot.traverse((o) => {
    if (!o.isMesh || !isVisual(o)) return;   // skip our own light effects
    const link = linkOf(o);
    const src = Array.isArray(o.material) ? o.material[0] : o.material;
    const name = (src && src.name) || '';
    const isCyl = o.geometry && o.geometry.type === 'CylinderGeometry';
    o.castShadow = true;
    o.receiveShadow = true;
    if (link === 'light_emitter_link') {
      o.material = matBulb; o.castShadow = false; o.receiveShadow = false;
    } else if (link === 'lamp_head_link' && isCyl) {
      o.material = matLens; o.castShadow = false;
    } else if (link === 'lamp_head_link') {
      o.material = matShade; o.castShadow = false;
    } else if (link === 'camera_link') {
      o.material = matBlack; o.castShadow = false;
    } else if (name.includes('chrome')) {
      o.material = matChrome;
    } else if (name.includes('black')) {
      o.material = matBlack;
    } else {
      o.material = matWhite;
    }
  });
}

function makeSpriteTexture(inner = 'rgba(255,255,255,1)', soft = 0.5) {
  const s = 128;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d');
  const rg = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  rg.addColorStop(0, inner);
  rg.addColorStop(soft, 'rgba(255,255,255,0.25)');
  rg.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = rg;
  g.fillRect(0, 0, s, s);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function makeBeam() {
  // Unit open cone: apex at origin, axis +X, length 1, radius 1 at x = 1.
  const geo = new THREE.ConeGeometry(1, 1, 64, 12, true);
  geo.translate(0, -0.5, 0);
  geo.rotateZ(Math.PI / 2);
  const mat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uColor: { value: new THREE.Color('#ffe7b0') },
      uOpacity: { value: 0.0 },
      uPower: { value: 1.6 },
      uTime: { value: 0 },
    },
    vertexShader: `
      varying float vU; varying vec3 vN; varying vec3 vV; varying float vAng;
      void main(){
        vU = position.x;
        vAng = atan(position.z, position.y);
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        vV = normalize(-mv.xyz);
        vN = normalize(normalMatrix * normal);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      uniform vec3 uColor; uniform float uOpacity; uniform float uPower; uniform float uTime;
      varying float vU; varying vec3 vN; varying vec3 vV; varying float vAng;
      void main(){
        float facing = abs(dot(normalize(vN), normalize(vV)));
        float core = pow(facing, uPower);
        float fade = pow(1.0 - clamp(vU, 0.0, 1.0), 1.7) * smoothstep(0.0, 0.06, vU);
        float shimmer = 0.88 + 0.12 * sin(vAng * 7.0 + uTime * 0.6 + vU * 9.0);
        float a = core * fade * shimmer * uOpacity;
        gl_FragColor = vec4(uColor * a, 1.0);
      }`,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.frustumCulled = false;
  mesh.renderOrder = 10;
  return mesh;
}

function makeDust(count = 260) {
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(count * 3);
  const col = new Float32Array(count * 3);
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
  const seeds = [];
  for (let i = 0; i < count; i++) {
    const r = Math.sqrt(Math.random()) * 0.95;
    seeds.push({ u: Math.random(), r, a: Math.random() * Math.PI * 2, sp: 0.006 + Math.random() * 0.018, sw: (Math.random() - 0.5) * 0.25, ph: Math.random() * 6.28 });
  }
  const mat = new THREE.PointsMaterial({
    size: 0.0075, map: makeSpriteTexture(), vertexColors: true, transparent: true,
    blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
  });
  const pts = new THREE.Points(geo, mat);
  pts.frustumCulled = false;
  pts.userData.seeds = seeds;
  return pts;
}

function attachLight() {
  const emitter = robot.links.light_emitter_link;
  const head = robot.links.lamp_head_link;

  // Spotlight sits at the bulb, aimed along the emitter's local +X.
  const spot = new THREE.SpotLight('#ffe7b0', 0, 0, THREE.MathUtils.degToRad(12), 0.55, 2);
  spot.position.set(0.0, 0, 0);
  spot.castShadow = true;
  spot.shadow.mapSize.set(1024, 1024);
  spot.shadow.camera.near = 0.04;
  spot.shadow.camera.far = 8;
  spot.shadow.bias = -0.0004;
  spot.shadow.normalBias = 0.01;
  emitter.add(spot);
  const target = new THREE.Object3D();
  target.position.set(1, 0, 0);
  emitter.add(target);
  spot.target = target;

  // Small warm point light so the inside of the shade and the head glow.
  const glow = new THREE.PointLight('#ffe7b0', 0, 0.9, 2);
  glow.position.set(-0.02, 0, 0);
  emitter.add(glow);

  const beam = makeBeam();
  const beamCore = makeBeam();
  beamCore.material.uniforms.uPower.value = 2.6;
  emitter.add(beam);
  emitter.add(beamCore);

  const halo = new THREE.Sprite(new THREE.SpriteMaterial({
    map: makeSpriteTexture('rgba(255,255,255,1)', 0.18), color: '#ffe7b0', transparent: true,
    blending: THREE.AdditiveBlending, depthWrite: false, opacity: 0,
  }));
  halo.position.set(0.01, 0, 0);
  halo.scale.setScalar(0.22);
  emitter.add(halo);

  const dust = makeDust();
  emitter.add(dust);

  Object.assign(lampFx, { emitter, head, spot, glow, beam, beamCore, halo, dust });
}

const manager = new THREE.LoadingManager();
const urdfLoader = new URDFLoader(manager);
urdfLoader.load(URDF_URL, (r) => {
  robot = r;
  lampRoot.add(robot);
  for (const j of JOINTS) robot.setJointValue(j, current.joints[j]);
  attachLight();
}, undefined, (err) => console.error('URDF load failed', err));
manager.onLoad = () => { if (robot) styleRobot(); };

// ------------------------------------------------------------------- state
const target = {
  joints: { ...REST_POSE },
  color: new THREE.Color('#ffe2a8'),
  intensity: 0.75,
  beam: 0.35,
};
const current = {
  joints: { ...REST_POSE },
  color: target.color.clone(),
  intensity: 0,
  beam: target.beam,
};
let haveState = false;

function applyState(msg) {
  if (msg.joints) {
    for (const j of JOINTS) {
      const v = msg.joints[j];
      if (typeof v === 'number' && Number.isFinite(v)) target.joints[j] = v;
    }
    if (!haveState) Object.assign(current.joints, target.joints);
  }
  const l = msg.light;
  if (l) {
    if (Array.isArray(l.color) && l.color.length === 3) target.color.setRGB(l.color[0], l.color[1], l.color[2], THREE.SRGBColorSpace);
    if (typeof l.intensity === 'number') target.intensity = THREE.MathUtils.clamp(l.intensity, 0, 1);
    if (typeof l.beam === 'number') target.beam = THREE.MathUtils.clamp(l.beam, 0, 1);
  }
  haveState = true;
}

// ---------------------------------------------------------------- post fx
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 0.42, 0.55, 0.86);
composer.addPass(bloom);
composer.addPass(new OutputPass());

// The side panel covers the right of the screen; shift the projection so the
// lamp stays centred in the visible area.
let viewShift = 0;
let viewShiftTarget = 0;
function updateViewOffset() {
  const w = window.innerWidth, h = window.innerHeight;
  if (Math.abs(viewShift) < 0.5) camera.clearViewOffset();
  else camera.setViewOffset(w, h, viewShift, 0, w, h);
}

let sizeW = 0, sizeH = 0;
function onResize() {
  const w = window.innerWidth, h = window.innerHeight;
  sizeW = w; sizeH = h;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
  composer.setSize(w, h);
  bloom.setSize(w, h);
  viewShiftTarget = panelHidden ? 0 : panelWidth() / 2;
}
window.addEventListener('resize', onResize);

function panelWidth() {
  const el = document.getElementById('panel');
  return el ? el.offsetWidth + 22 : 0;
}

// --------------------------------------------------------------- animation
const clock = new THREE.Clock();
const tmpColor = new THREE.Color();
let engagement = 0;

function frame() {
  const dt = Math.min(clock.getDelta(), 0.1);
  const t = clock.elapsedTime;
  if (window.innerWidth !== sizeW || window.innerHeight !== sizeH) onResize();

  // Light smoothing to hide network jitter (server already shapes motion).
  const kJ = 1 - Math.exp(-dt * 18);
  const kL = 1 - Math.exp(-dt * 9);
  for (const j of JOINTS) current.joints[j] += (target.joints[j] - current.joints[j]) * kJ;
  current.color.lerp(target.color, kL);
  current.intensity += (target.intensity - current.intensity) * kL;
  current.beam += (target.beam - current.beam) * kL;

  if (robot) {
    for (const j of JOINTS) robot.setJointValue(j, current.joints[j]);
  }

  if (lampFx.spot) updateLight(dt, t);

  // eye glyph breathes with attention
  engagement += ((hud.attention && hud.attention.engaged ? 1 : (hud.attention && hud.attention.score) || 0) - engagement) * (1 - Math.exp(-dt * 2));
  if (glyphMat) glyphMat.opacity = 0.16 + 0.22 * engagement + 0.04 * Math.sin(t * 0.8);

  viewShift += (viewShiftTarget - viewShift) * (1 - Math.exp(-dt * 6));
  updateViewOffset();

  controls.update();
  composer.render();
  requestAnimationFrame(frame);
}

function updateLight(dt, t) {
  const { spot, glow, beam, beamCore, halo, dust } = lampFx;
  const I = current.intensity;
  const full = THREE.MathUtils.lerp(BEAM_MIN, BEAM_MAX, current.beam);
  const half = full / 2;
  const tanH = Math.tan(half);
  const flicker = 1 + 0.015 * Math.sin(t * 13.1) * Math.sin(t * 7.3);

  spot.color.copy(current.color);
  spot.intensity = SPOT_CANDELA * I * I * flicker;  // perceptual-ish curve
  spot.angle = half * 1.08;
  spot.visible = I > 0.002;

  glow.color.copy(current.color);
  glow.intensity = 0.25 * I;

  // Narrow beams read as more concentrated.
  const conc = THREE.MathUtils.lerp(1.35, 0.8, current.beam);
  beam.scale.set(BEAM_LEN, BEAM_LEN * tanH, BEAM_LEN * tanH);
  beam.material.uniforms.uColor.value.copy(current.color);
  beam.material.uniforms.uOpacity.value = 0.11 * I * conc * flicker;
  beam.material.uniforms.uTime.value = t;
  beamCore.scale.set(BEAM_LEN * 0.8, BEAM_LEN * 0.8 * tanH * 0.45, BEAM_LEN * 0.8 * tanH * 0.45);
  beamCore.material.uniforms.uColor.value.copy(current.color);
  beamCore.material.uniforms.uOpacity.value = 0.09 * I * conc;
  beamCore.material.uniforms.uTime.value = t * 1.3;
  beam.visible = beamCore.visible = I > 0.002;

  tmpColor.copy(current.color);
  matBulb.emissive.copy(tmpColor);
  matBulb.emissiveIntensity = 0.06 + 4.0 * I * flicker;
  matLens.emissive.copy(tmpColor);
  matLens.emissiveIntensity = 0.02 + 0.75 * I;
  halo.material.color.copy(tmpColor);
  halo.material.opacity = 0.35 * I;
  halo.scale.setScalar(0.13 + 0.07 * I);

  // dust motes drifting down the beam
  const seeds = dust.userData.seeds;
  const pos = dust.geometry.attributes.position.array;
  const col = dust.geometry.attributes.color.array;
  const L = BEAM_LEN * 0.75;
  for (let i = 0; i < seeds.length; i++) {
    const s = seeds[i];
    s.u += s.sp * dt;
    s.a += s.sw * dt;
    if (s.u > 1) { s.u = 0.02; s.r = Math.sqrt(Math.random()) * 0.95; }
    const x = s.u * L;
    const rr = s.r * x * tanH;
    pos[i * 3] = x;
    pos[i * 3 + 1] = Math.cos(s.a) * rr + Math.sin(t * 0.3 + s.ph) * 0.004;
    pos[i * 3 + 2] = Math.sin(s.a) * rr;
    const tw = 0.55 + 0.45 * Math.sin(t * 1.7 + s.ph * 3.0);
    const f = I * tw * Math.pow(1 - s.u, 1.4) * Math.min(1, s.u * 12) * (1 - s.r * 0.6) * 1.6;
    col[i * 3] = current.color.r * f;
    col[i * 3 + 1] = current.color.g * f;
    col[i * 3 + 2] = current.color.b * f;
  }
  dust.geometry.attributes.position.needsUpdate = true;
  dust.geometry.attributes.color.needsUpdate = true;
}

// ---------------------------------------------------------------------- HUD
const $ = (id) => document.getElementById(id);
const hud = { mode: 'DORMANT', attention: null, memory: [], breakthrough: false };
const logLines = [];
let subtitleTimer = 0;
let heardTimer = 0;
let memorySeen = null;   // null until the first ledger arrives

function setMode(m) {
  const el = $('mode');
  const mode = String(m || '').toUpperCase();
  hud.mode = mode;
  el.dataset.mode = mode;
  el.textContent = mode.toLowerCase();
}

function writeSubtitle(el, text) {
  el.textContent = text;
  el.dataset.text = text;   // duplicated by ::before/::after for the glitch slices
}

function setSubtitle(text) {
  const el = $('subtitle');
  clearTimeout(subtitleTimer);
  if (!text) { el.classList.remove('show'); return; }
  if (hud.breakthrough) {
    // The second voice cuts in: no crossfade.
    writeSubtitle(el, text);
    el.classList.remove('glitch-in');
    void el.offsetWidth;   // restart the entry glitch
    el.classList.add('show', 'glitch-in');
    return;
  }
  if (el.classList.contains('show') && el.textContent !== text) {
    el.classList.remove('show');
    subtitleTimer = setTimeout(() => { writeSubtitle(el, text); el.classList.add('show'); }, 280);
  } else {
    writeSubtitle(el, text);
    requestAnimationFrame(() => el.classList.add('show'));
  }
}

function setBreakthrough(on) {
  on = !!on;
  if (on === hud.breakthrough) return;
  hud.breakthrough = on;
  const el = $('subtitle');
  document.body.classList.toggle('breakthrough', on);
  el.classList.toggle('breakthrough', on);
  if (on) { el.classList.remove('glitch-in'); void el.offsetWidth; el.classList.add('glitch-in'); }
  else el.classList.remove('glitch-in');
}

function setHeard(text) {
  const el = $('heard');
  clearTimeout(heardTimer);
  if (!text) { el.classList.remove('show'); return; }
  $('heard-text').textContent = text;
  el.classList.add('show');
  heardTimer = setTimeout(() => el.classList.remove('show'), 12000);
}

function setAttention(a) {
  hud.attention = a;
  const faces = a.faces | 0;
  $('att-faces').textContent = String(faces);
  $('att-looking').textContent = a.looking ? 'upon it' : (faces ? 'averted' : 'none');
  const score = THREE.MathUtils.clamp(Number(a.score) || 0, 0, 1);
  $('att-score').style.width = `${Math.round(score * 100)}%`;
  $('eye').classList.toggle('engaged', !!a.engaged);
  eyeOpenTarget = a.engaged ? 1 : a.looking ? 0.6 : faces ? 0.28 : 0.06;
}

// The SVG eye lid opens and closes smoothly.
let eyeOpen = 0.06, eyeOpenTarget = 0.06;
function animateEye() {
  eyeOpen += (eyeOpenTarget - eyeOpen) * 0.12;
  const h = 2 + eyeOpen * 26;           // lid control-point offset
  const d = `M14 34 Q60 ${34 - h} 106 34 Q60 ${34 + h} 14 34 Z`;
  $('eye-outline').setAttribute('d', d);
  $('eye-clip-path').setAttribute('d', d);
  requestAnimationFrame(animateEye);
}
animateEye();

function setMemory(items) {
  const ul = $('memory');
  ul.textContent = '';
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'nothing recorded';
    ul.appendChild(li);
  }
  const next = new Set();
  for (const it of items) {
    const s = String(it);
    next.add(s);
    const li = document.createElement('li');
    li.textContent = s;
    if (memorySeen && !memorySeen.has(s)) li.classList.add('fresh');
    ul.appendChild(li);
  }
  memorySeen = next;
  ul.scrollTop = ul.scrollHeight;
}

function addEvent(line) {
  const now = new Date();
  const ts = now.toTimeString().slice(0, 8);
  logLines.push([ts, String(line)]);
  while (logLines.length > 8) logLines.shift();
  const ol = $('log');
  ol.textContent = '';
  for (const [t, l] of logLines) {
    const li = document.createElement('li');
    const tm = document.createElement('time');
    tm.textContent = t;
    const sp = document.createElement('span');
    sp.textContent = l;
    li.append(tm, sp);
    ol.appendChild(li);
  }
}

function setLatency(obj) {
  const el = $('latency');
  el.textContent = '';
  const entries = Object.entries(obj || {});
  if (!entries.length) { el.textContent = '—'; return; }
  for (const [k, v] of entries) {
    const ms = Number(v);
    const span = document.createElement('span');
    if (ms > 1500) span.className = 'slow';
    span.append(`${k} `);
    const b = document.createElement('b');
    b.textContent = Number.isFinite(ms) ? (ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}`) : String(v);
    span.append(b);
    if (Number.isFinite(ms) && ms < 1000) span.append('ms');
    el.appendChild(span);
  }
}

function setGoal(g) {
  const el = $('goal');
  if (g) { el.textContent = g; el.classList.remove('idle'); }
  else { el.textContent = 'no design in progress'; el.classList.add('idle'); }
}

function setCamera(b64) {
  const box = $('cam');
  if (!b64) { box.classList.add('empty'); return; }
  $('cam-img').src = `data:image/jpeg;base64,${b64}`;
  box.classList.remove('empty');
}

function applyHud(m) {
  if ('mode' in m) setMode(m.mode);
  if ('breakthrough' in m) setBreakthrough(m.breakthrough);
  if ('subtitle' in m) setSubtitle(m.subtitle || '');
  if ('heard' in m) setHeard(m.heard || '');
  if (m.attention && typeof m.attention === 'object') setAttention(m.attention);
  if (Array.isArray(m.memory)) setMemory(m.memory);
  if ('camera' in m) setCamera(m.camera);
  if (m.event) addEvent(m.event);
  if (m.latency && typeof m.latency === 'object') setLatency(m.latency);
  if ('goal' in m) setGoal(m.goal || '');
}

// -------------------------------------------------------------- websocket
let ws = null;
let retry = 0;
const conn = $('conn');

function setConn(state, label) {
  conn.dataset.state = state;
  conn.querySelector('span').textContent = label;
}

function connect() {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/viz`;
  setConn('connecting', retry ? 'reconnecting…' : 'connecting…');
  try { ws = new WebSocket(url); } catch (e) { scheduleReconnect(); return; }
  ws.onopen = () => { retry = 0; setConn('open', 'connected'); };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'state') applyState(msg);
    else if (msg.type === 'hud') applyHud(msg);
  };
  ws.onclose = () => { ws = null; scheduleReconnect(); };
  ws.onerror = () => { try { ws && ws.close(); } catch { /* ignore */ } };
}

function scheduleReconnect() {
  setConn('connecting', 'reconnecting…');
  const delay = Math.min(8000, 400 * 2 ** retry) * (0.8 + Math.random() * 0.4);
  retry = Math.min(retry + 1, 6);
  setTimeout(connect, delay);
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return true; }
  return false;
}

// ----------------------------------------------------------------- inputs
const input = $('say-input');
$('say').addEventListener('submit', (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  if (send({ type: 'text', text })) input.value = '';
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') input.blur();
  e.stopPropagation();
});

let pttDown = false;
function setPtt(down) {
  if (down === pttDown) return;
  pttDown = down;
  document.body.classList.toggle('ptt', down);
  send({ type: 'ptt', down });
}

let panelHidden = false;
try { panelHidden = localStorage.getItem('architecture.panelHidden') === '1'; } catch { /* ignore */ }
if (qs.has('clean')) panelHidden = true;
function applyPanel() {
  document.body.classList.toggle('clean', panelHidden);
  viewShiftTarget = panelHidden ? 0 : panelWidth() / 2;
}
applyPanel();
viewShift = viewShiftTarget;

function typing() {
  const a = document.activeElement;
  return a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.isContentEditable);
}

window.addEventListener('keydown', (e) => {
  if (typing() || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.code === 'Space') {
    e.preventDefault();
    if (!e.repeat) setPtt(true);
  } else if (e.code === 'KeyH') {
    panelHidden = !panelHidden;
    try { localStorage.setItem('architecture.panelHidden', panelHidden ? '1' : '0'); } catch { /* ignore */ }
    applyPanel();
  } else if (e.code === 'KeyF') {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else document.documentElement.requestFullscreen().catch(() => {});
  } else if (e.code === 'KeyC') {
    camera.position.copy(CAM_HOME);
    controls.target.copy(CAM_TARGET);
  } else if (e.key === 'Enter' || e.key === '/') {
    if (!panelHidden) { e.preventDefault(); input.focus(); }
  }
});
window.addEventListener('keyup', (e) => {
  if (e.code === 'Space') setPtt(false);
});
window.addEventListener('blur', () => setPtt(false));

// ------------------------------------------------------------------ start
onResize();
connect();
requestAnimationFrame(frame);

// Small debugging handle.
window.__arch = { scene, camera, controls, get robot() { return robot; }, target, current };
