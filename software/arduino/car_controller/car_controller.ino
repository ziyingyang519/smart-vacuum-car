/*
  G09 智慧吸塵車 Arduino 控制程式 v2（依電路組接線圖修改）
  ---------------------------------------------------------------
  從樹莓派（USB 序列埠 115200）收一行指令：
    F180  前進，速度 180（0~255）     B180  後退
    L180  原地左轉                    R180  原地右轉
    S     停止
    V / v 吸塵馬達 開 / 關

  接線（依電路組接線圖）：
    L298N IN1 ← D9   IN2 ← D6     → OUT1/OUT2 → 馬達 A（暫定左輪）
    L298N IN3 ← D5   IN4 ← D3     → OUT3/OUT4 → 馬達 B（暫定右輪）
    繼電器 IN ← D2
    Arduino GND ↔ 電池 / L298N GND（一定要共地！）
  ※ D3、D5、D6、D9 都是 PWM 腳，直接在 IN 腳做 PWM 調速，
    所以 L298N 上 ENA、ENB 的跳線帽「要插著」。
  ※ 沒有接超音波和伺服馬達，所以這版不使用。

  安全設計：
    1. 斷線保護：超過 FAILSAFE_MS 沒收到移動指令 → 停車（網頁移動中每 0.15 秒會重送）
    2. 慢慢加減速：避免急煞前翻、起步打滑
*/

// ====== 腳位（接線不同就改這裡）======
const int A_IN1 = 9, A_IN2 = 6;   // 馬達 A（左輪）
const int B_IN1 = 5, B_IN2 = 3;   // 馬達 B（右輪）
const int VAC_PIN = 2;            // 繼電器

// ====== 可調參數 ======
const bool VAC_ACTIVE_HIGH = true;     // 按 V 繼電器沒反應、反而按 v 才開 → 改 false
const bool INVERT_LEFT  = false;       // 左輪轉反了就改 true
const bool INVERT_RIGHT = false;       // 右輪轉反了就改 true
const bool SWAP_LR      = false;       // 按 A 卻往右轉（左右馬達對調）→ 改 true
const float TURN_RATIO  = 0.7;         // 原地轉彎用直行速度的幾成
const int  MIN_PWM      = 70;          // 馬達開始轉動的最低 PWM（實測後調整）
const int  RAMP_STEP    = 9;           // 每 10 ms 速度最多變化量（約 0.3 秒從全速到停）
const unsigned long FAILSAFE_MS = 400; // 這麼久沒收到移動指令 → 停車

// ====== 狀態 ======
char motion = 'S';
int targetL = 0, targetR = 0, curL = 0, curR = 0;
unsigned long lastMoveCmdMs = 0, lastRampMs = 0;
char buf[16];
byte bufLen = 0;

void setVacuum(bool on) { digitalWrite(VAC_PIN, (on == VAC_ACTIVE_HIGH) ? HIGH : LOW); }

// 在 IN 腳做 PWM：正轉 IN1=PWM、IN2=0；反轉 IN1=0、IN2=PWM；停止兩腳都 0
void driveOne(int in1, int in2, int val, bool invert) {
  if (invert) val = -val;
  if (val == 0) { analogWrite(in1, 0); analogWrite(in2, 0); return; }
  int v = map(abs(val), 1, 255, MIN_PWM, 255);
  if (val > 0) { analogWrite(in2, 0); analogWrite(in1, v); }
  else         { analogWrite(in1, 0); analogWrite(in2, v); }
}

void driveBoth(int left, int right) {
  if (SWAP_LR) { int t = left; left = right; right = t; }
  driveOne(A_IN1, A_IN2, left,  INVERT_LEFT);
  driveOne(B_IN1, B_IN2, right, INVERT_RIGHT);
}

int stepToward(int cur, int target) {
  if (cur < target) return min(cur + RAMP_STEP, target);
  if (cur > target) return max(cur - RAMP_STEP, target);
  return cur;
}

void setMotion(char m, int speed) {
  int t = (int)(speed * TURN_RATIO);
  motion = m;
  switch (m) {
    case 'F': targetL =  speed; targetR =  speed; break;
    case 'B': targetL = -speed; targetR = -speed; break;
    case 'L': targetL = -t;     targetR =  t;     break;
    case 'R': targetL =  t;     targetR = -t;     break;
    default:  targetL = 0; targetR = 0; motion = 'S';
  }
}

void handleLine(char *s) {
  char c = s[0];
  int speed = (s[1] != 0) ? constrain(atoi(s + 1), 0, 255) : 180;
  switch (c) {
    case 'F': case 'B': case 'L': case 'R':
      setMotion(c, speed); lastMoveCmdMs = millis(); break;
    case 'S': setMotion('S', 0); break;
    case 'V': setVacuum(true);  break;
    case 'v': setVacuum(false); break;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(A_IN1, OUTPUT); pinMode(A_IN2, OUTPUT);
  pinMode(B_IN1, OUTPUT); pinMode(B_IN2, OUTPUT);
  pinMode(VAC_PIN, OUTPUT); setVacuum(false);
  driveBoth(0, 0);
  Serial.println("READY");
}

void loop() {
  // 1. 讀指令（一行一行）
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (bufLen) { buf[bufLen] = 0; handleLine(buf); bufLen = 0; }
    } else if (bufLen < sizeof(buf) - 1) {
      buf[bufLen++] = ch;
    } else {
      bufLen = 0;                        // 太長＝亂碼，丟掉
    }
  }

  unsigned long now = millis();

  // 2. 斷線保護
  if (motion != 'S' && now - lastMoveCmdMs > FAILSAFE_MS) setMotion('S', 0);

  // 3. 每 10 ms 讓速度往目標靠近一步
  if (now - lastRampMs >= 10) {
    lastRampMs = now;
    curL = stepToward(curL, targetL);
    curR = stepToward(curR, targetR);
    driveBoth(curL, curR);
  }
}
