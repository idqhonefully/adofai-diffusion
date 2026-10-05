import json
import re


class ADOLevelData:
    class exception(Exception):
        def __init__(self, message):
            super().__init__(message)

    def __init__(self, data):
        self.data = data
        self.index = 0
        self.max_index = len(data) - 1
        self.success = False
        self.type = ""
        self.result = {}
        self._logger = None

    def set_logger(self, logger):
        self._logger = logger

    def _log(self, tag, message):
        if self._logger:
            self._logger(tag, message)

    @staticmethod
    def new(file: str) -> "ADOLevelData":
        s = open(file, "r", encoding="UTF-8-SIG")
        d = s.read()
        s.close()
        return ADOLevelData(d)

    def encode(self) -> str:
        output = (
            "{\n\t"
            + json.dumps({self.type: self.result[self.type]}, separators=[", ", ": "])[
                :-1
            ][1:]
            + ", "
        )
        output += (
            json.dumps(
                {"settings": self.result["settings"]},
                indent="\t",
                separators=[", ", ": "],
            )[:-2][1:]
            + ', \n\t"actions": [ \n'
        )
        for out in self.result["actions"]:
            output += "\t\t"
            output += json.dumps(out, separators=[", ", ": "]) + ",\n"
            pass
        output = output[:-2] + '\n\t], \n\t"decorations": [ \n'
        for out in self.result["decorations"]:
            output += "\t\t"
            output += json.dumps(out, separators=[", ", ": "]) + ",\n"
            pass
        output = output[:-2] + "\n\t]\n}"
        return output

    def _parse_json(self) -> dict:
        """优先用标准 JSON 解析，必要时兼容 ADOFAI 常见的尾随逗号。

        手写解析器无法处理 ``null``：它会把 ``null`` 元素直接吞掉，
        导致 ``"position": [null, -100]`` 被解析成 ``[-100]``（数组错位），
        因此改为先尝试标准库。只有在标准解析失败时才按 Adofai_AutoCat 的做法
        去掉 ", }" / ", ]" 形式的尾随逗号重试，最后才回退到内置解析器，
        保持对历史畸形文件的兼容性。
        """
        text = self.data
        if text[:1] == "\ufeff":
            text = text[1:]
        parsed = None
        try:
            parsed = json.loads(text, strict=False)
        except Exception:
            try:
                parsed = json.loads(re.sub(r",(?=\s*[}\]])", "", text), strict=False)
            except Exception as exc:
                self._log("DECODE", f"标准 JSON 解析失败({exc}), 回退到内置解析器")
                self.index = 0
                return self.task_object()
        if not isinstance(parsed, dict):
            raise ADOLevelData.exception("it not adofai syntax")
        return parsed

    def decode(self) -> None:
        self._log("DECODE", "=" * 60)
        self._log("DECODE", "开始解析谱面文件")
        self._log("DECODE", f"文件总长度: {len(self.data)} 字符")

        if not self.data.lstrip("\ufeff \t\r\n").startswith("{"):
            raise ADOLevelData.exception("it not adofai syntax")

        self.result = self._parse_json()
        self._log("DECODE", f"顶层对象键: {list(self.result.keys())}")

        if not "decorations" in self.result:
            self.result["decorations"] = []
        if not "actions" in self.result:
            self.result["actions"] = []
        if "pathData" in self.result:
            self.type = "pathData"
            self._log("DECODE", f"谱面类型: pathData, 长度: {len(self.result['pathData'])}")
        elif "angleData" in self.result:
            self.type = "angleData"
            self._log("DECODE", f"谱面类型: angleData, 轨道数: {len(self.result['angleData'])}")
        else:
            raise ADOLevelData.exception("can't find tile data type")

        actions = self.result.get("actions", [])
        event_types = {}
        for a in actions:
            et = a.get("eventType", "Unknown")
            event_types[et] = event_types.get(et, 0) + 1
        self._log("DECODE", f"事件统计: {event_types}")
        self._log("DECODE", f"settings.bpm = {self.result.get('settings', {}).get('bpm', 'N/A')}")

        if self.index >= self.max_index:
            pass

    def skip_space_and_tab(self) -> None:
        while self.max_index >= self.index:
            i = self.data[self.index]
            if i == " " or i == "\t":
                self.index += 1
            else:
                break

    def task_type(self) -> str:
        return self.task_string()

    def task_string(self) -> str:
        self.index += 1
        this_result = ""
        while 1:
            i = self.data[self.index]
            if self.index >= self.max_index:
                break
            if i == "\\":
                this_result += self.data[self.index : self.index + 1]
                self.index += 2
            elif i == "\n":
                this_result += "\\n"
                self.index += 1
            elif i == "\t":
                this_result += "\\t"
                self.index += 1
            elif i == '"':
                self.index += 1
                break
            else:
                this_result += i
                self.index += 1
        return this_result

    def task_number(self) -> float | int:
        this_result = ""
        is_float = False
        while 1:
            i = self.data[self.index]
            if i in "-0123456789":
                this_result += i
            elif i == "." or i == "E" or i == "e" or i == "+":
                this_result += i
                is_float = True
            else:
                break
            self.index += 1
        return float(this_result) if is_float else int(this_result)

    def task_array(self) -> list:
        self.index += 1
        this_result = []
        while 1:
            self.skip_space_and_tab()
            i = self.data[self.index]
            if i == '"':
                this_result.append(self.task_string())
            elif i in "-0123456789":
                this_result.append(self.task_number())
            elif self.data[self.index : self.index + 5] == "false":
                this_result.append(False)
                self.index += 5
            elif self.data[self.index : self.index + 4] == "true":
                this_result.append(True)
                self.index += 4
            elif i == "[":
                this_result.append(self.task_array())
            elif i == "{":
                this_result.append(self.task_object())
            elif i == "]":
                self.index += 1
                break
            else:
                self.index += 1
        return this_result

    def task_object(self) -> dict:
        self.index += 1
        t = None
        this_result = {}
        while 1:
            self.skip_space_and_tab()
            i = self.data[self.index]
            if i == "\n":
                self.index += 1
            elif t == None:
                if i == '"':
                    t = self.task_type()
                elif i == "}":
                    self.index += 1
                    break
                else:
                    self.index += 1
            else:
                if i == '"':
                    this_result[t] = self.task_string()
                    t = None
                elif i in "-0123456789":
                    this_result[t] = self.task_number()
                    t = None
                elif self.data[self.index : self.index + 5] == "false":
                    this_result[t] = False
                    self.index += 5
                    t = None
                elif self.data[self.index : self.index + 4] == "true":
                    this_result[t] = True
                    self.index += 4
                    t = None
                elif i == "[":
                    this_result[t] = self.task_array()
                    t = None
                elif i == "{":
                    this_result[t] = self.task_object()
                    t = None
                else:
                    self.index += 1
        return this_result
