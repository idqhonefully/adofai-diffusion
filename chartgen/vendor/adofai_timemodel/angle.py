from .reader import ADOLevelData


class ADOAngle():
    Angle_Correspondence = {
        "p": 15.0,
        "J": 30.0,
        "E": 45.0,
        "T": 60.0,
        "o": 75.0,
        "U": 90.0,
        "q": 105.0,
        "G": 120.0,
        "Q": 135.0,
        "H": 150.0,
        "W": 165.0,
        "L": 180.0,
        "x": 195.0,
        "N": 210.0,
        "Z": 225.0,
        "F": 240.0,
        "V": 255.0,
        "D": 270.0,
        "Y": 285.0,
        "B": 300.0,
        "C": 315.0,
        "M": 330.0,
        "A": 345.0,
        "R": 360.0,
        "!": 999.0,
    }

    TWO_PLANET_PAUSE_BEAT_DIFF = -1
    THREE_PLANET_PAUSE_BEAT_DIFF = -1

    def __init__(self, ald: ADOLevelData):
        self._logger = None
        try:
            self.angleData = ald.result["angleData"]
            self._log("INIT", f"使用 angleData, 原始长度: {len(self.angleData)}")
        except KeyError:
            path_data = ald.result["pathData"]
            self.angleData = [self.Angle_Correspondence[char] for char in path_data]
            self._log("INIT", f"使用 pathData 转换, 原始长度: {len(self.angleData)}, pathData: {path_data[:50]}...")

        self.floorNum = len(self.angleData)
        self.settings = ald.result['settings']
        self.actions = ald.result['actions']
        self._log("INIT", f"总轨道数(floorNum): {self.floorNum}")
        self._log("INIT", f"settings: bpm={self.settings.get('bpm')}, 其他键={list(self.settings.keys())}")

    def _is_action_active(self, action):
        active = action.get('active')
        if active is False:
            self._log("ACTIVE_SKIP", f"跳过事件: floor={action['floor']}, type={action.get('eventType')} (active=false)")
            return False
        return True

    def set_logger(self, logger):
        self._logger = logger

    def _log(self, tag, message):
        if self._logger:
            self._logger(tag, message)

    def getRotateAngle(self):
        """计算每一格的角行程（官方 scrLevelMaker.CalculateFloorEntryTimes 的复刻）。

        结果写入 ``self.originRotateAngleList``（长度与 ``angleData`` 相同，中旋格为
        999）。约定：下标 j 上的数值表示“到第 j 格为止这一格的角行程”，中旋格没有
        独立按键因此置 999 并在后续被移除；第一个实体格固定为 0，使时间轴从第一个
        按键开始计时（与 Macro 现有约定一致）。
        """
        self._log("ROTATE", "=" * 60)
        self._log("ROTATE", "开始计算旋转角度")

        angleData = self.angleData
        floorNum = len(angleData)

        self._log("ROTATE", f"原始 angleData 前20个: {angleData[:20]}")
        self._log("ROTATE", f"中旋轨道(999)位置: {[i for i, a in enumerate(angleData) if a == 999][:20]}...")

        # 归一化：中旋 999 原样保留，其余角度映射到 (0, 360]
        self.angleData[:] = [
            999.0 if a == 999 else (a + 360.0 if a <= 0 else float(a))
            for a in angleData
        ]
        angleData = self.angleData
        self._log("ROTATE", f"标准化后 angleData 前20个: {angleData[:20]}")

        # ---------- 1. 每格的入/出方向角（官方 InstantiateFloatFloors） ----------
        entry = [0.0] * (floorNum + 1)
        exitAngle = [0.0] * (floorNum + 1)
        isMidspin = [False] * (floorNum + 1)
        entry[0] = 270.0  # 官方 FirstEntryAngle = 3π/2
        for j in range(floorNum):
            if angleData[j] == 999:
                exitAngle[j] = entry[j]  # 中旋格出角 = 入角
                isMidspin[j] = True
            else:
                exitAngle[j] = (90.0 - angleData[j]) % 360.0
            entry[j + 1] = (exitAngle[j] + 180.0) % 360.0
        exitAngle[floorNum] = (entry[floorNum] + 180.0) % 360.0

        # ---------- 2. 收集会影响角行程的事件 ----------
        # 注意：ADOFAI 的 action.floor 就是 angleData 的下标（0 基）
        twirlAt = set()
        planetsAt = {}
        pauseAt = {}
        holdAt = {}
        freeRoamAt = {}
        for action in self.actions:
            if not self._is_action_active(action):
                continue
            floor = action.get("floor")
            if not isinstance(floor, int) or floor < 0:
                continue
            eventType = action.get("eventType")
            if eventType == "Twirl":
                twirlAt.add(floor)
            elif eventType == "MultiPlanet":
                planetsAt[floor] = 3 if action.get("planets") == "ThreePlanets" else 2
            elif eventType == "Pause":
                try:
                    pauseAt[floor] = float(action.get("duration", 0.0))
                except (TypeError, ValueError):
                    pass
            elif eventType == "Hold":
                try:
                    holdAt[floor] = int(action.get("duration", 0))
                except (TypeError, ValueError):
                    pass
            elif eventType == "FreeRoam":
                try:
                    freeRoamAt[floor] = float(action.get("duration", 0.0))
                except (TypeError, ValueError):
                    pass

        def movedDegrees(startAngle, endAngle, clockwise):
            """官方 scrMisc.GetAngleMoved（角度制，结果落在 [0, 360)）。"""
            delta = (endAngle - startAngle) * (1.0 if clockwise else -1.0)
            return delta % 360.0

        def inverseAnglePerBeat(planets):
            """官方 scrMisc.GetInverseAnglePerBeatMultiplanet（角度制）。"""
            if planets <= 2:
                return 0.0
            return 180.0 * (planets - 2.0) / planets

        # 事件生效前的默认角行程（官方 ApplyCoreEvents 里的 floorAngleLength，供 FreeRoam 使用）
        defaultLength = []
        for j in range(floorNum):
            length = movedDegrees(entry[j], exitAngle[j], True)
            if length <= 1e-6 or length >= 360.0 - 1e-6:
                length = 0.0 if isMidspin[j] else 360.0
            defaultLength.append(length)

        # ---------- 3. 逐格计算角行程 ----------
        rotateAngleList = [999.0] * floorNum
        ccw = False
        planetNum = 2
        calcLog = []

        for j in range(floorNum):
            if j in twirlAt:
                ccw = not ccw
            if j in planetsAt:
                planetNum = planetsAt[j]

            inverse = inverseAnglePerBeat(planetNum)
            offset = -inverse if ccw else inverse
            if isMidspin[j]:
                offset = 0.0
            elif j > 0 and isMidspin[j - 1] and planetNum > 2:
                # 紧接中旋之后的格子：官方把 offset 抵消成 0（等价于二球角长）
                offset -= (-(360.0 + inverse) if ccw else (360.0 + inverse))

            startAngle = entry[j] + offset
            endAngle = exitAngle[j] + (offset if isMidspin[j] else 0.0)
            travel = movedDegrees(startAngle, endAngle, not ccw)
            if travel <= 1e-6 or travel >= 360.0 - 1e-6:
                travel = 0.0 if isMidspin[j] else 360.0

            # 暂停：官方忽略中旋格上的 Pause
            if not isMidspin[j] and j in pauseAt:
                travel += pauseAt[j] * 180.0

            # 长按：官方对中旋格同样生效（duration 拍 -> duration*2 拍角长）
            if j in holdAt and holdAt[j] >= 0:
                travel += holdAt[j] * 360.0

            # 自由漫游
            if j in freeRoamAt:
                duration = int(freeRoamAt[j])
                if duration >= 2:
                    beats = defaultLength[j] / 180.0
                    travel += max(beats, duration - beats) * 180.0

            rotateAngleList[j] = travel
            if j < 12 or (j in pauseAt or j in holdAt):
                calcLog.append(
                    f"  floor {j}: angle={angleData[j]}, midspin={isMidspin[j]}, "
                    f"ccw={ccw}, planets={planetNum}, travel={travel:.6f}, beat={travel / 180.0:.6f}"
                )

        # 中旋格本身没有按键：它上面额外的角行程（例如中旋格上的 Hold）发生在
        # “离开中旋格之后、抵达下一按键之前”，因此并入前一个实体格；
        # 第一个实体格之前的角行程整体丢弃（Macro 时间轴从第一个按键开始计时）
        lastReal = -1
        for j in range(floorNum):
            if isMidspin[j]:
                if lastReal >= 0:
                    rotateAngleList[lastReal] += rotateAngleList[j]
                rotateAngleList[j] = 999.0
            else:
                lastReal = j
        for j in range(floorNum):
            if not isMidspin[j]:
                rotateAngleList[j] = 0.0
                break

        self._log("ROTATE", "基础旋转角度计算完成, 长度: %d" % len(rotateAngleList))
        for line in calcLog[:15]:
            self._log("ROTATE", line)
        if len(calcLog) > 15:
            self._log("ROTATE", f"  ... ({len(calcLog) - 15} more lines)")

        self.originRotateAngleList = rotateAngleList
        self.rotateAngleList = self._removeUselessTiles(rotateAngleList)

        self._log("ROTATE", f"最终 rotateAngleList (去999后) 长度: {len(self.rotateAngleList)}")
        self._log("ROTATE", f"前20个: {[round(x, 2) for x in self.rotateAngleList[:20]]}")

        return self.rotateAngleList

    def _removeUselessTiles(self, l):
        result = [angle for angle in l if angle != 999]
        removed = [i for i, angle in enumerate(l) if angle == 999]
        if removed:
            self._log("FILTER", f"移除中旋轨道(999)位置: {removed[:20]}{'...' if len(removed)>20 else ''}, 共{len(removed)}个")
        return result

    def getPlanetNumList(self):
        actions = self.actions
        planetNumList = {0: 2}
        for i in actions:
            if i.get('eventType') == 'MultiPlanet':
                if not self._is_action_active(i):
                    continue
                if i['planets'] == 'ThreePlanets':
                    planetNumList[i['floor']] = 3
                else:
                    planetNumList[i['floor']] = 2
        return planetNumList

    def getAutoTileList(self):
        actions = self.actions
        autoTileList = []
        _autoTileEvents = {}
        lastState = False
        for i in actions:
            if i.get('eventType') == 'AutoPlayTiles':
                if not self._is_action_active(i):
                    continue
                if i['enabled'] != lastState:
                    _autoTileEvents[i['floor']] = i['enabled']
                    lastState = i['enabled']
        if lastState == True:
            _autoTileEvents[self.floorNum + 1] = False
        autoBegin = 0
        for i, enabled in sorted(_autoTileEvents.items()):
            if enabled:
                autoBegin = i
            else:
                autoTileList.append([autoBegin-1, i-1])
        return autoTileList

    def getHoldDict(self):
        actions = self.actions
        hold_dict = {}
        skipped = []
        for i in actions:
            if i.get('eventType') == 'Hold':
                if not self._is_action_active(i):
                    skipped.append(i['floor'])
                    continue
                hold_dict[i['floor']] = i['duration'] * 360
        if skipped:
            self._log("HOLD", f"跳过禁用Hold事件: floors={skipped}")
        return hold_dict

    def _is_hold_followed_by_autoplay_midspin(self, hold_floor):
        next_floor = hold_floor + 1
        if next_floor >= self.floorNum:
            return False

        if self.angleData[next_floor] != 999:
            return False

        has_auto_start = False
        for a in self.actions:
            if a.get('eventType') == 'AutoPlayTiles':
                if not self._is_action_active(a):
                    continue
                if a['floor'] == next_floor and a['enabled'] == True:
                    has_auto_start = True
                    break

        if not has_auto_start:
            return False

        self._log("HOLD_FLOOR", f"  -> [特殊处理] Hold@{hold_floor} 的下一格({next_floor})是中旋+AutoPlay开")
        self._log("HOLD_FLOOR", f"     Hold 变为普通点击，不加入长按列表")
        return True

    def getAutoPlayFloors(self):
        autoTileList = self.getAutoTileList()
        auto_floors = set()

        for start, end in autoTileList:
            new_start = start
            new_end = end
            for i, angle in enumerate(self.angleData):
                if angle == 999:
                    if start > i:
                        new_start -= 1
                    if end > i:
                        new_end -= 1
            for f in range(new_start, new_end):
                auto_floors.add(f)

        self._log("AUTOPLAY", f"去999后的AutoPlay区间索引: {sorted(auto_floors)}")
        return auto_floors

    def getHoldFloors(self):
        self._log("HOLD_FLOOR", "=" * 60)
        self._log("HOLD_FLOOR", "开始计算压缩后的长按轨道索引")

        if not hasattr(self, 'pressIntervalList'):
            self._log("HOLD_FLOOR", "pressIntervalList 未计算，先调用 getPressIntervalList()")
            self.getPressIntervalList()

        hold_dict = self.getHoldDict()
        if not hold_dict:
            self._log("HOLD_FLOOR", "没有长按事件，返回空列表")
            return []

        auto_tile_list = self.getAutoTileList()
        self._log("HOLD_FLOOR", f"原始长按: {sorted(hold_dict.keys())}")
        self._log("HOLD_FLOOR", f"自动方块区间: {auto_tile_list}")
        self._log("HOLD_FLOOR", f"原始 angleData 长度: {len(self.angleData)}")
        self._log("HOLD_FLOOR", f"中旋轨道(999)位置: {[i for i, a in enumerate(self.angleData) if a == 999]}")

        adjusted = []
        step_log = []

        for floor in sorted(hold_dict):
            if self._is_hold_followed_by_autoplay_midspin(floor):
                step_log.append(f"\n[处理 floor={floor + 1}]")
                step_log.append(f"  -> 跳过：下一格是中旋+AutoPlay开，Hold变为普通点击")
                continue

            new_floor = floor
            midspin_count = 0
            for i, angle in enumerate(self.angleData):
                if angle == 999 and floor > i:
                    new_floor -= 1
                    midspin_count += 1

            step_log.append(f"\n[处理 floor={floor + 1}]")
            step_log.append(f"  原始floor={floor + 1}")
            if midspin_count > 0:
                step_log.append(f"  -> 原始floor({floor + 1})之前有中旋轨道 {midspin_count} 个")
                step_log.append(f"  -> 去999后索引 = {new_floor}")

            dropped = False
            for start, end in auto_tile_list:
                new_start = start
                new_end = end
                for i, angle in enumerate(self.angleData):
                    if angle == 999:
                        if start > i:
                            new_start -= 1
                        if end > i:
                            new_end -= 1

                if new_start <= new_floor < new_end:
                    dropped = True
                    step_log.append(
                        f"  -> 位于自动方块区间 [{start + 1},{end + 1}) -> 去999后[{new_start},{new_end})，丢弃!")
                    break
                elif new_floor >= new_end:
                    old = new_floor
                    new_floor -= new_end - new_start
                    step_log.append(
                        f"  -> 去999后索引({old}) >= 自动方块结束({new_end})，减去区间长度 {new_end - new_start}: {old} -> {new_floor}")

            if not dropped:
                step_log.append(f"  未被丢弃，当前去999后索引 = {new_floor}")

                if 0 <= new_floor < len(self.pressIntervalList):
                    adjusted.append(new_floor)
                    step_log.append(f"  -> 有效! 加入结果: {new_floor}")
                else:
                    step_log.append(f"  -> 越界! 0 <= {new_floor} < {len(self.pressIntervalList)} ? 否，丢弃")

        for line in step_log:
            self._log("HOLD_FLOOR", line)

        self._log("HOLD_FLOOR", f"\n最终结果: {adjusted}")
        self._log("HOLD_FLOOR", f"共 {len(adjusted)} 个有效长按轨道")
        return adjusted

    def getBeatList(self):
        if not hasattr(self, 'originRotateAngleList'):
            self.getRotateAngle()

        rotateAngle = self.originRotateAngleList
        beatList = []
        for angle in rotateAngle:
            if angle == 999:
                beatList.append(angle)
            else:
                beatList.append(angle / 180)
        self.originBeatList = beatList
        beatList2 = self._removeUselessTiles(beatList)
        self.beatList = beatList2

        self._log("BEAT", f"拍子列表 (去999后) 长度: {len(self.beatList)}")
        self._log("BEAT", f"前20个: {[round(x, 4) for x in self.beatList[:20]]}")
        return beatList2

    def _buildSpeedSegments(self, base_bpm=None):
        """按角度把 SetSpeed 造成的 BPM 变化切成若干分段。

        返回 ``[(seg_start_angle, seg_end_angle, bpm), ...]``，覆盖 ``[0, 总角度]``；
        谱面没有 SetSpeed 事件时只有一段，BPM 即 ``settings['bpm']``。
        ``base_bpm`` 用于换算 Multiplier 类速度事件(默认取谱面 BPM)。
        """
        if base_bpm is None:
            base_bpm = self.settings['bpm']
        if not hasattr(self, 'originRotateAngleList'):
            self.getRotateAngle()

        tile_angles = []
        cum_angle = [0.0]
        for angle in self.originRotateAngleList:
            span = 0.0 if angle == 999 else angle
            tile_angles.append(span)
            cum_angle.append(cum_angle[-1] + span)
        total_angle = cum_angle[-1]

        current_bpm = self.settings['bpm']

        speed_events = []
        for action in self.actions:
            if action.get('eventType') != 'SetSpeed':
                continue
            if not self._is_action_active(action):
                continue
            if action['floor'] >= len(tile_angles):
                self._log("ABS_BEAT",
                          f"  WARNING: SetSpeed floor {action['floor']} >= tile count {len(tile_angles)}, skip")
                continue
            abs_angle = cum_angle[action['floor']] + float(action.get('angleOffset', 0))
            clamped = max(0.0, min(abs_angle, total_angle))
            if clamped != abs_angle:
                self._log("ABS_BEAT",
                          f"  WARNING: floor {action['floor']} angleOffset clamped: {abs_angle:.2f} -> {clamped:.2f}")
            speed_events.append((clamped, action))
        speed_events.sort(key=lambda item: item[0])

        self._log("ABS_BEAT", f"SetSpeed事件(按角度排序):")
        for ang, act in speed_events[:10]:
            self._log("ABS_BEAT",
                      f"  angle={ang:.2f}, floor={act['floor']}, type={act['speedType']}, value={act.get('beatsPerMinute', act.get('bpmMultiplier', 'N/A'))}")

        segments = []
        pos_angle = 0.0
        seg_log = []
        for abs_angle, action in speed_events:
            if abs_angle > pos_angle:
                segments.append((pos_angle, abs_angle, current_bpm))
                seg_log.append(f"  segment: {pos_angle:.2f} -> {abs_angle:.2f}, BPM={current_bpm}")
            old_bpm = current_bpm
            if action['speedType'] == 'Bpm':
                current_bpm = action['beatsPerMinute']
            elif action['speedType'] == 'Multiplier':
                current_bpm *= action['bpmMultiplier']
            seg_log.append(f"  @ angle={abs_angle:.2f}: BPM {old_bpm} -> {current_bpm}")
            pos_angle = abs_angle
        if total_angle > pos_angle:
            segments.append((pos_angle, total_angle, current_bpm))
            seg_log.append(f"  segment: {pos_angle:.2f} -> {total_angle:.2f}, BPM={current_bpm}")

        for line in seg_log[:15]:
            self._log("ABS_BEAT", line)
        return segments

    def getAbsBeatList(self, bpm=-1):
        self._log("ABS_BEAT", "=" * 60)
        self._log("ABS_BEAT", "开始计算绝对节拍")

        if bpm < 0:
            bpm = self.settings['bpm']
        self._log("ABS_BEAT", f"基准 BPM: {bpm}")

        if not hasattr(self, 'originBeatList'):
            self.getBeatList()

        base_bpm = bpm

        tile_angles = []
        cum_angle = [0.0]
        for angle in self.originRotateAngleList:
            span = 0.0 if angle == 999 else angle
            tile_angles.append(span)
            cum_angle.append(cum_angle[-1] + span)
        total_angle = cum_angle[-1]

        self._log("ABS_BEAT", f"总角度: {total_angle:.2f}")
        self._log("ABS_BEAT", f"累计角度前10个: {[round(x, 2) for x in cum_angle[:11]]}")

        segments = self._buildSpeedSegments(base_bpm)

        beats = [0.0] * len(self.originBeatList)
        detail_log = []

        for floor in range(len(self.originBeatList)):
            if self.originBeatList[floor] == 999:
                beats[floor] = 999
                continue

            tile_start = cum_angle[floor]
            tile_end = cum_angle[floor + 1]
            tile_beats = 0.0

            for seg_start, seg_end, seg_bpm in segments:
                ov_start = max(seg_start, tile_start)
                ov_end = min(seg_end, tile_end)
                if ov_end > ov_start:
                    contrib = (ov_end - ov_start) / 180.0 * (base_bpm / seg_bpm)
                    tile_beats += contrib
                    if floor < 5:
                        detail_log.append(
                            f"  floor {floor}: seg [{seg_start:.1f},{seg_end:.1f}] BPM={seg_bpm}, overlap=[{ov_start:.1f},{ov_end:.1f}], contrib={contrib:.6f}")

            beats[floor] = tile_beats
            if floor < 5:
                detail_log.append(f"  floor {floor}: total_beats={tile_beats:.6f}")

        for line in detail_log:
            self._log("ABS_BEAT", line)

        autoTileList = self.getAutoTileList()
        self._log("ABS_BEAT", f"自动方块区间: {autoTileList}")

        auto_log = []
        for start, end in autoTileList:
            if start >= len(beats):
                continue
            if end <= self.floorNum:
                auto_log.append(f"  区间 [{start+1},{end+1}): 这些floor将被标记为AutoPlay，不生成按键")
                for j in range(start, end):
                    if j < len(beats):
                        if beats[j] != 999:
                            auto_log.append(f"    floor {j+1}: {beats[j]:.4f} 拍 -> 保留在列表中(AutoPlay)")
            else:
                auto_log.append(f"  区间 [{start+1},{end+1}): 持续到结束，舍去 floor {start+1} 之后")
                for j in range(start, len(beats)):
                    beats[j] = 999

        for line in auto_log:
            self._log("ABS_BEAT", line)

        absoluteBeatList = self._removeUselessTiles(beats)
        self.absBeatList = absoluteBeatList
        self.basebpm = bpm

        self._log("ABS_BEAT", f"最终绝对节拍列表长度: {len(self.absBeatList)}")
        self._log("ABS_BEAT", f"前20个: {[round(x, 4) for x in self.absBeatList[:20]]}")
        return absoluteBeatList

    def getBpmTimeline(self):
        """BPM 随时间的变化点(供节奏提示等窗口实时显示)。

        时间轴与 :meth:`getMacroKeyInfo` 的 ``press_time`` 一致(单位 ms)，
        返回 ``[(time_ms, bpm), ...]``，按时间升序，首项为谱面起始处的 BPM。
        谱面没有 SetSpeed 事件时只返回一项(``settings['bpm']``)。
        """
        if not hasattr(self, 'pressIntervalList'):
            self.getPressIntervalList()
        if not hasattr(self, 'basebpm'):
            self.basebpm = self.settings['bpm']

        base_bpm = self.basebpm
        segments = self._buildSpeedSegments(base_bpm)
        ms_per_beat = 60000.0 / base_bpm if base_bpm > 0 else 0.0

        timeline = []
        beats = 0.0
        for start_angle, end_angle, seg_bpm in segments:
            # 本段起点对应的演奏时间 = 之前累计的“基准拍数” x 每拍毫秒数
            timeline.append((beats * ms_per_beat, seg_bpm))
            if seg_bpm > 0:
                beats += (end_angle - start_angle) / 180.0 * (base_bpm / seg_bpm)

        self.bpmTimeline = timeline
        self._log("BPM_LIST", "BPM 变化点(时间轴与按键时间一致):")
        for time_ms, seg_bpm in timeline[:20]:
            self._log("BPM_LIST", f"  {time_ms:.2f}ms -> BPM={seg_bpm:g}")
        if len(timeline) > 20:
            self._log("BPM_LIST", f"  ... (共 {len(timeline)} 段)")
        return timeline

    def getPressIntervalList(self):
        if not hasattr(self, 'absBeatList'):
            self.getAbsBeatList()

        if not hasattr(self, 'basebpm'):
            self.basebpm = self.settings['bpm']

        beatPressTime = 60000 / self.basebpm
        pressIntervalList = []

        for beat in self.absBeatList:
            pressIntervalList.append(beatPressTime * beat)

        self.pressIntervalList = pressIntervalList

        self._log("INTERVAL", f"按键间隔列表长度: {len(self.pressIntervalList)}")
        self._log("INTERVAL", f"beatPressTime (1拍@basebpm={self.basebpm}): {beatPressTime:.4f}ms")
        self._log("INTERVAL", f"前20个间隔: {[round(x, 2) for x in self.pressIntervalList[:20]]}")
        return pressIntervalList

    def getMacroPressIntervalList(self):
        if not hasattr(self, 'pressIntervalList'):
            self.getPressIntervalList()
        self.macroPressIntervalList = self.pressIntervalList
        return self.pressIntervalList

    def getMacroCumulativeTimes(self):
        if not hasattr(self, 'macroPressIntervalList'):
            self.getMacroPressIntervalList()

        cumulative_times = [0]
        for interval in self.pressIntervalList:
            cumulative_times.append(cumulative_times[-1] + interval)

        self.macroCumulativeTimes = cumulative_times

        self._log("CUMULATIVE", f"累计时间列表长度: {len(self.macroCumulativeTimes)}")
        self._log("CUMULATIVE", f"前20个: {[round(x, 2) for x in self.macroCumulativeTimes[:20]]}")
        return cumulative_times

    def getKeyLimiterList(self):
        result = []
        for action in self.actions:
            if action.get('eventType') != 'KeyLimiter':
                continue
            if not self._is_action_active(action):
                continue
            try:
                floor = int(action.get('floor', 0))
                limit = int(action.get('limit', 1))
            except (TypeError, ValueError):
                self._log("KEYLIMITER", f"跳过无效 KeyLimiter: {action}")
                continue
            if limit <= 0:
                limit = 1
            result.append((floor, limit))
            self._log("KEYLIMITER", f"KeyLimiter @floor={floor}, limit={limit}")
        result.sort(key=lambda item: item[0])
        return result

    def getMacroKeyInfo(self):
        self._log("KEYINFO", "=" * 60)
        self._log("KEYINFO", "开始生成 MacroKeyInfo")

        if not hasattr(self, 'pressIntervalList'):
            self.getPressIntervalList()

        hold_floors = set(self.getHoldFloors())
        auto_floors = set(self.getAutoPlayFloors())
        intervals = self.pressIntervalList

        hold_dict = self.getHoldDict()
        auto_tile_list = self.getAutoTileList()
        skip_release_floors = set()

        for hold_floor in sorted(hold_dict.keys()):
            new_floor = hold_floor
            for idx, angle in enumerate(self.angleData):
                if angle == 999 and hold_floor > idx:
                    new_floor -= 1

            in_autoplay = False
            for start, end in auto_tile_list:
                new_start = start
                new_end = end
                for idx, angle in enumerate(self.angleData):
                    if angle == 999:
                        if start > idx: new_start -= 1
                        if end > idx: new_end -= 1

                if new_start <= new_floor < new_end:
                    in_autoplay = True
                    break
                elif new_floor >= new_end:
                    new_floor -= new_end - new_start

            if in_autoplay or new_floor in auto_floors:
                release_raw_idx = hold_floor
                release_new_idx = release_raw_idx
                for idx, angle in enumerate(self.angleData):
                    if angle == 999 and release_raw_idx > idx:
                        release_new_idx -= 1
                skip_release_floors.add(release_new_idx)
                self._log("KEYINFO", f"  Hold@{hold_floor} 在AutoPlay中开始，释放拍索引{release_new_idx} 跳过")

        cumulative = [0]
        for interval in intervals:
            cumulative.append(cumulative[-1] + interval)

        new_to_raw = {}
        new_idx = 0
        for i, angle in enumerate(self.angleData):
            if angle == 999:
                continue
            new_to_raw[new_idx] = i + 1
            new_idx += 1

        self._log("KEYINFO", f"轨道数: {len(intervals)}, 累计时间点数: {len(cumulative)}")
        self._log("KEYINFO", f"长按轨道索引: {sorted(hold_floors)}")
        self._log("KEYINFO", f"AutoPlay轨道索引: {sorted(auto_floors)}")
        self._log("KEYINFO", f"Hold释放拍跳过索引: {sorted(skip_release_floors)}")

        key_info_list = []
        detail_log = []

        for i in range(len(intervals)):
            actual_floor = new_to_raw.get(i, '?')

            if i in auto_floors:
                detail_log.append(f"  floor {i}: AutoPlay区间，跳过 (实际位置floor {actual_floor}, 时间仍流逝)")
                continue

            if i in skip_release_floors:
                detail_log.append(f"  floor {i}: Hold释放拍(AutoPlay期间开始)，跳过 (实际位置floor {actual_floor})")
                continue

            if i in hold_floors:
                if i == 0:
                    release_time = cumulative[2] if len(cumulative) > 2 else cumulative[1]
                    key_info_list.append({
                        'raw_idx': i,
                        'press_time': cumulative[1],
                        'release_time': release_time,
                        'is_hold': True,
                    })
                    detail_log.append(
                        f"  floor {i}: 第0层长按 -> 独立按键, press={cumulative[1]:.2f}, release={release_time:.2f} (实际位置floor {actual_floor})")
                elif (i - 1) in hold_floors:
                    key_info_list.append({
                        'raw_idx': i - 1,
                        'press_time': cumulative[i],
                        'release_time': cumulative[i + 1],
                        'is_hold': True,
                    })
                    detail_log.append(
                        f"  floor {i}: 相邻长按 -> 新独立按键, press={cumulative[i]:.2f}, release={cumulative[i + 1]:.2f} (实际位置floor {actual_floor})")
                else:
                    if key_info_list:
                        old_release = key_info_list[-1].get('release_time')
                        key_info_list[-1]['release_time'] = cumulative[i + 1]
                        key_info_list[-1]['is_hold'] = True
                        detail_log.append(
                            f"  floor {i}: 长按 -> 延长前一个按键 release: {old_release} -> {cumulative[i + 1]:.2f} (实际位置floor {actual_floor})")
                    else:
                        detail_log.append(f"  floor {i}: 长按但无前一个按键，跳过 (实际位置floor {actual_floor})")
                continue

            key_info_list.append({
                'raw_idx': i,
                'press_time': cumulative[i + 1],
                'release_time': None,
                'is_hold': False,
            })
            detail_log.append(f"  floor {i}: 普通按键, press={cumulative[i + 1]:.2f} (实际位置floor {actual_floor})")

        for item in key_info_list:
            raw_idx = item.get('raw_idx')
            item['floor'] = max(0, new_to_raw.get(raw_idx, raw_idx) - 1)

        for line in detail_log:
            self._log("KEYINFO", line)

        self.macroKeyInfo = key_info_list
        self._log("KEYINFO", f"最终 key_info_list 长度: {len(key_info_list)}")
        self._log("KEYINFO", f"完整列表:")
        for idx, item in enumerate(key_info_list):
            release = item['release_time'] if item['release_time'] is not None else 'None'
            actual_floor = new_to_raw.get(item['raw_idx'], '?')
            self._log("KEYINFO",
                      f"  floor {idx}: raw_idx={item['raw_idx']}, press={item['press_time']:.2f}, release={release}, is_hold={item['is_hold']} (实际位置floor {actual_floor})")
        return key_info_list
