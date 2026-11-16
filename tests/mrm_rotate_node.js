/* R148 Web 灯箱旋转公式护栏（纯函数逻辑，对应 app.js 的 ((LB.rot||0)+90)%360）。
 * 旋转只改显示、不写原文件；每次 +90°，4 次回正，取值恒为 0/90/180/270。 */
'use strict';
function rotateStep(a) { return ((Number(a) || 0) + 90) % 360; }

var cases = [
  [0, 90], [90, 180], [180, 270], [270, 0], [360, 90],
  [-90, 0], [undefined, 90], [45, 135],
];
var ok = true;
for (var i = 0; i < cases.length; i++) {
  var got = rotateStep(cases[i][0]);
  var want = cases[i][1];
  if (got !== want) {
    console.error('FAIL rotateStep(' + cases[i][0] + ') = ' + got + ' want ' + want);
    ok = false;
  }
}
// 4 次回到 0
var v = 0; for (var k = 0; k < 4; k++) v = rotateStep(v);
if (v !== 0) { console.error('FAIL 4x rotate != 0 (' + v + ')'); ok = false; }
// 取值必须是 0/90/180/270：从合法状态 {0,90,180,270} 出发，输出仍在该集合
var states = [0, 90, 180, 270];
for (var s = 0; s < states.length; s++) {
  var x = rotateStep(states[s]);
  if ([0, 90, 180, 270].indexOf(x) < 0) {
    console.error('FAIL value set from ' + states[s] + ' -> ' + x);
    ok = false;
  }
}
console.log(ok ? 'R148 web rotate formula OK' : 'R148 web rotate formula FAILED');
process.exit(ok ? 0 : 1);
