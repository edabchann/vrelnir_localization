import contextlib
import csv
import datetime
import re
import os
import platform
from .ast_javascript import Acorn, JSSyntaxError
from typing import Any
from urllib.parse import quote
from zipfile import ZipFile as zf, ZIP_DEFLATED
from aiofiles import open as aopen
from pathlib import Path

import asyncio
import json
import httpx
import shutil
import subprocess
import time
import webbrowser
import stat

from .consts import *
from .log import logger
from .parse_text import *

# from .download import *

LOGGER_COLOR = logger.opt(colors=True)


class ProjectDOL:
    """本地化主类"""

    def __init__(self, type_: str = "common"):
        with open(DIR_JSON_ROOT / "blacklists.json", "r", encoding="utf-8") as fp:
            self._blacklists: dict[str, list] = json.load(fp)

        self._type: str = type_
        self._version: str = None
        self._mention_name = "" if self._type == "common" else "dev"
        self._commit: dict[str, Any] = None
        self._acorn = Acorn()
        self._gitgud_token = os.getenv("GITGUD_TOKEN")
        self.DIR_GIT_REPO = DIR_TEMP_ROOT / f"dol_{self._type}_git"

        if FILE_COMMITS.exists():
            with open(FILE_COMMITS, "r", encoding="utf-8") as fp:
                self._commit: dict[str, Any] = json.load(fp)

        self._is_latest = False
        self._paratranz_file_lists: list[Path] = None
        self._raw_dicts_file_lists: list[Path] = None
        self._game_texts_file_lists: list[Path] = None

    def _init_dirs(self, version: str):
        """创建目标文件夹"""
        os.makedirs(DIR_TEMP_ROOT, exist_ok=True)
        os.makedirs(DIR_RAW_DICTS / self._type / version / "csv", exist_ok=True)

    """ 获取最新版本 """

    async def fetch_latest_version(self, is_quiet: bool = True):
        # 根据 type 决定拉取哪个分支
        branch = self.get_type("master", "dev")
        repo_url = "https://gitgud.io/Vrelnir/degrees-of-lewdity.git"

        try:
            if not self.DIR_GIT_REPO.exists():
                logger.info(f"🚚 正在通过 Git 浅克隆仓库分支 [{branch}]...")
                # 使用 subprocess.run 调用系统自带的 git 命令
                process = subprocess.run(
                    [
                        "git",
                        "clone",
                        "--depth=1",
                        "-b",
                        branch,
                        repo_url,
                        str(self.DIR_GIT_REPO),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            else:
                logger.info(f"🔄 本地 Git 仓库已存在，正在执行 git pull 更新...")
                process = subprocess.run(
                    ["git", "pull"],
                    cwd=str(self.DIR_GIT_REPO),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

            if process.returncode != 0:
                logger.error(f"Git 操作失败: {process.stderr}")
                raise RuntimeError(
                    "本地 Git 命令执行失败，请检查系统是否安装 git 或网络是否能挂代理连通 gitgud。"
                )

        except Exception as e:
            logger.error(f"通过 Git 获取仓库失败: {e}")
            raise e

        # 直接从本地刚拉下来的 Git 目录里读取 version 文件
        local_version_file = self.DIR_GIT_REPO / "version"
        if not local_version_file.exists():
            raise FileNotFoundError(
                f"在 Git 仓库中未找到 version 文件: {local_version_file}"
            )

        with open(local_version_file, "r", encoding="utf-8") as fp:
            self._version = fp.read().strip()

        if not is_quiet:
            logger.info(f"当前{self._mention_name}仓库最新版本: {self._version}")

        self._init_dirs(self._version)

    """ 下载源码 """

    async def download_from_gitgud(self):
        """从 gitgud 下载源仓库文件"""
        if not self._version:
            await self.fetch_latest_version()
        if self._is_latest:  # 下载慢，是最新就不要重复下载了
            dol_path_zip = DIR_ROOT / f"dol{self._mention_name}.zip"
            if dol_path_zip.exists():
                with contextlib.suppress(shutil.Error, FileNotFoundError):
                    shutil.move(dol_path_zip, DIR_TEMP_ROOT)
                await self.unzip_latest_repository()
                return
        await self.fetch_latest_repository()
        await self.unzip_latest_repository()

    async def fetch_latest_repository(self):
        """获取最新仓库内容（改用本地 Git 目录打包，并模拟 GitGud 官方的二级目录结构避免路径丢失）"""
        logger.info(f"===== 开始获取最新{self._mention_name}仓库内容 ...")

        save_path: Path = DIR_TEMP_ROOT / f"dol{self._mention_name}.zip"

        if not self.DIR_GIT_REPO.exists():
            raise RuntimeError("Git 目录不存在，请先运行版本检查！")

        logger.info(f"📦 正在将本地 Git 代码打包为 {save_path.name} ...")

        # 统一规范顶层文件夹名称，使其与原脚本中 consts.py 定义的路径前缀完全对齐
        top_dir_name = (
            "degrees-of-lewdity-master"
            if self._type == "common"
            else "degrees-of-lewdity-dev"
        )

        try:
            if save_path.exists():
                os.remove(save_path)

            with zf(save_path, "w", ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(self.DIR_GIT_REPO):
                    # 忽略 .git 目录
                    if ".git" in dirs:
                        dirs.remove(".git")
                    for file in files:
                        file_path = Path(root) / file
                        # 核心修改：在写入 zip 时，保持相对路径并在最前面垫上 top_dir_name 文件夹
                        arcname = Path(top_dir_name) / file_path.relative_to(
                            self.DIR_GIT_REPO
                        )
                        zipf.write(file_path, arcname)

            logger.info(
                f"##### 最新{self._mention_name}仓库内容已从本地 Git 打包获取完毕! \n"
            )
        except Exception as e:
            logger.error(f"打包本地 Git 仓库失败: {e}")
            raise e

    async def unzip_latest_repository(self):
        """解压到本地并自动赋予 Linux 下编译工具的可执行权限"""
        logger.info(f"===== 开始解压{self._mention_name}最新仓库内容 ...")

        # 1. 执行原生解压逻辑
        with zf(DIR_TEMP_ROOT / f"dol{self._mention_name}.zip") as zfp:
            zfp.extractall(DIR_ROOT)
        logger.info(f"##### 最新{self._mention_name}仓库内容已解压! \n")

        # 2. 核心修复：如果是 Linux 系统，解压出来的瞬间立刻对工具链强行 chmod +x
        if platform.system() == "Linux":
            logger.info("🐧 检测到 Linux 环境，正在自动配置 Tweego 编译权限...")

            # 定位二进制文件与脚本路径
            tweego_exe = (
                "tweego_linux86"
                if PLATFORM_ARCHITECTURE == "32bit"
                else "tweego_linux64"
            )
            tweego_exe_file = self.game_dir / "devTools" / "tweego" / tweego_exe
            tweego_compile_sh = self.game_dir / "compile.sh"

            # 给 tweego 二进制加权
            if tweego_exe_file.exists():
                tweego_exe_file.chmod(tweego_exe_file.stat().st_mode | stat.S_IEXEC)
                logger.info(f"\t- 已赋予权限: {tweego_exe}")

            # 给 compile.sh 脚本加权
            if tweego_compile_sh.exists():
                tweego_compile_sh.chmod(tweego_compile_sh.stat().st_mode | stat.S_IEXEC)
                logger.info("\t- 已赋予权限: compile.sh")

    async def patch_format_js(self):
        """汉化 format.js"""
        logger.info(f"===== 开始替换 format.js ...")
        shutil.copyfile(
            DIR_DATA_ROOT / "jsmodule" / "format.js",
            DIR_GAME_ROOT_COMMON
            / "devTools"
            / "tweego"
            / "storyFormats"
            / "sugarcube-2"
            / "format.js",
        )
        logger.info(f"##### format.js 已替换！\n")

    """ 创建生肉词典 """

    async def create_dicts(self):
        """创建字典"""
        await self._fetch_all_text_files()
        await self._create_all_text_files_dir()
        await self._process_texts()

    async def _fetch_all_text_files(self):
        """获取所有文本文件"""
        logger.info(f"===== 开始获取{self._mention_name}所有文本文件位置 ...")
        self._game_texts_file_lists = []
        if self._type == "common":
            texts_dir = DIR_GAME_TEXTS_COMMON
        else:
            texts_dir = DIR_GAME_TEXTS_DEV
        for root, dir_list, file_list in os.walk(texts_dir):
            dir_name = Path(root).absolute().name
            for file in file_list:
                if not file.endswith(SUFFIX_TWEE) and not file.endswith(SUFFIX_JS):
                    continue

                if dir_name not in self._blacklists:
                    self._game_texts_file_lists.append(Path(root).absolute() / file)
                elif (
                    not self._blacklists[dir_name] or file in self._blacklists[dir_name]
                ):
                    continue
                else:
                    self._game_texts_file_lists.append(Path(root).absolute() / file)

        logger.info(f"##### {self._mention_name}所有文本文件位置已获取 !\n")

    async def _create_all_text_files_dir(self):
        """创建目录防报错"""
        if not self._version:
            await self.fetch_latest_version()
        if self._type == "common":
            dir_name = DIR_GAME_ROOT_COMMON_NAME
        else:
            dir_name = DIR_GAME_ROOT_DEV_NAME
        for file in self._game_texts_file_lists:
            target_dir = file.parent.parts[file.parts.index(dir_name) + 1 :]
            target_dir_csv = (
                DIR_RAW_DICTS / self._type / self._version / "csv"
            ).joinpath(*target_dir)
            if not target_dir_csv.exists():
                os.makedirs(target_dir_csv, exist_ok=True)
            target_dir_json = (
                DIR_RAW_DICTS / self._type / self._version / "json"
            ).joinpath(*target_dir)
            if not target_dir_json.exists():
                os.makedirs(target_dir_json, exist_ok=True)

    async def _process_texts(self):
        """处理翻译文本为键值对"""
        logger.info(f"===== 开始处理{self._mention_name}翻译文本为键值对 ...")
        tasks = [
            self._process_for_gather(idx, file)
            for idx, file in enumerate(self._game_texts_file_lists)
        ]
        await asyncio.gather(*tasks)
        logger.info(f"##### {self._mention_name}翻译文本已处理为键值对 ! \n")

    async def _process_for_gather(self, idx: int, file: Path):
        target_file = (
            Path().joinpath(*file.parts[file.parts.index("game") + 1 :]).with_suffix("")
        )

        with open(file, "r", encoding="utf-8") as fp:
            lines = fp.readlines()
        with open(file, "r", encoding="utf-8") as fp:
            content = fp.read()
        if file.name.endswith(SUFFIX_TWEE):
            pt = ParseTextTwee(lines, file)
            pre_bool_list = pt.pre_parse_set_run()
        elif file.name.endswith(SUFFIX_JS):
            pt = ParseTextJS(lines, file)
            target_file = f"{target_file}.js"
        else:
            return
        able_lines = pt.parse()
        if file.name.endswith(SUFFIX_TWEE) and pt.pre_bool_list:
            able_lines = [
                True if pre_bool_list[idx] or line else False
                for idx, line in enumerate(able_lines)
            ]

        if not any(able_lines):
            logger.warning(f"\t- ***** 文件 {file} 无有效翻译行 !")
            return
        try:
            results_lines_csv = [
                (f"{idx_ + 1}_{'_'.join(self._version[2:].split('.'))}|", _.strip())
                for idx_, _ in enumerate(lines)
                if able_lines[idx_]
            ]
            results_lines_json = await self._build_json_results_with_passage(
                lines,
                able_lines,
                content,
                file.__str__().split("\\game\\")[-1].split("/game/")[-1],
            )
        except IndexError:
            logger.error(f"lines: {len(lines)} - parsed: {len(able_lines)}| {file}")
            results_lines_csv = None
            results_lines_json = None
        if results_lines_csv:
            with open(
                DIR_RAW_DICTS
                / self._type
                / self._version
                / "csv"
                / "game"
                / f"{target_file}.csv",
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as fp:
                csv.writer(fp).writerows(results_lines_csv)
        if results_lines_json:
            with open(
                DIR_RAW_DICTS
                / self._type
                / self._version
                / "json"
                / "game"
                / f"{target_file}.json",
                "w",
                encoding="utf-8",
                newline="",
            ) as fp:
                json.dump(results_lines_json, fp, ensure_ascii=False, indent=2)

    async def _build_json_results_with_passage(
        self, lines: list[str], able_lines: list[bool], content: str, file: str
    ) -> list[dict]:
        """导出成带 passage 注释的行文本"""
        results_lines_json = []
        passage_name = None
        pos_relative = None
        pos_global = 0
        for idx, line in enumerate(lines):
            if line.startswith("::"):
                pos_relative = 0
                tmp_ = line.lstrip(":: ")
                if "[" not in line:
                    passage_name = tmp_.strip()
                else:
                    for idx_, char in enumerate(tmp_):
                        if char != "[":
                            continue
                        passage_name = tmp_[:idx_].strip()
                        break
                    else:
                        raise

            if able_lines[idx]:
                pos_start = 0
                if line != line.lstrip():  # 前面的 \t \s 也要算上
                    for char in line:
                        if char == line.strip()[0]:
                            break
                        pos_start += 1
                results_lines_json.append(
                    {
                        "passage": passage_name,  # 非 twee 文件为 null
                        "filepath": file,
                        "key": f"{idx + 1}_{'_'.join(self._version[2:].split('.'))}|",
                        "original": line.strip(),
                        "translation": "",
                        "pos": (
                            pos_relative + pos_start
                            if pos_relative is not None
                            else pos_global + pos_start
                        ),  # 非 twee 文件为 null
                    }
                )
                if content[pos_global + pos_start] != line.lstrip()[0]:
                    logger.error(
                        f"pos可能不对！{file} | {passage_name} | {line}".replace(
                            "\t", "\\t"
                        ).replace("\n", "\\n")
                    )
            if pos_relative is not None and not line.startswith("::"):
                pos_relative += len(line)
            pos_global += len(line)
        return results_lines_json

    """ 去重生肉词典 """

    async def shear_off_repetition(self):
        """目前仅限世扩"""
        logger.info(f"===== 开始去重{self._mention_name}文本 ...")
        # 不要对原版调用去重
        if self._type == "common":
            raise Exception("不要对原版调用去重")

        for root, dir_list, file_list in os.walk(
            DIR_RAW_DICTS / self._type / self._version / "csv" / "game"
        ):
            if "失效词条" in root or "移除文件" in root:
                continue

            for file in file_list:
                common_file_path = (
                    DIR_PARATRANZ
                    / "common"
                    / "utf8"
                    / Path().joinpath(*(Path(root) / file).split("game//")[1])
                )
                if not common_file_path.exists():
                    continue
                mod_file_path = Path(root) / file

                with open(mod_file_path, "r", encoding="utf-8") as fp:
                    mod_data = list(csv.reader(fp))

                with open(common_file_path, "r", encoding="utf-8") as fp:
                    common_data = list(csv.reader(fp))
                    common_ens: dict = {
                        row[-2] if len(row) > 2 else row[1]: idx_
                        for idx_, row in enumerate(common_data)
                    }  # 旧英文: 旧英文行键

                # mod 中的键也在原版中，直接删掉
                for idx, row in enumerate(mod_data.copy()):
                    if row[-1] in common_ens:
                        mod_data[idx] = None

                mod_data = [_ for _ in mod_data if _]
                if not mod_data:
                    os.remove(mod_file_path)
                    continue

                with open(mod_file_path, "w", encoding="utf-8-sig", newline="") as fp:
                    csv.writer(fp).writerows(mod_data)

            if not os.listdir(Path(root)):
                shutil.rmtree(Path(root))
        logger.info(f"##### {self._mention_name}所有文本已去重 !\n")

    """ 替换生肉词典 """

    async def update_dicts(self):
        """更新字典"""
        if not self._version:
            await self.fetch_latest_version()
        logger.info(f"===== 开始更新{self._mention_name}字典 ...")
        file_mapping: dict = {}
        for root, dir_list, file_list in os.walk(
            DIR_PARATRANZ / self._type / "utf8"
        ):  # 导出的旧字典
            if "失效词条" in root or "移除文件" in root:
                continue
            for file in file_list:
                file_mapping[Path(root).absolute() / file] = (
                    DIR_RAW_DICTS
                    / self._type
                    / self._version
                    / "csv"
                    / "game"
                    / Path(root).relative_to(DIR_PARATRANZ / self._type / "utf8")
                    / file,
                    DIR_RAW_DICTS
                    / self._type
                    / self._version
                    / "json"
                    / "game"
                    / Path(root).relative_to(DIR_PARATRANZ / self._type / "utf8")
                    / f'{file.removesuffix(".csv")}.json',
                )

        tasks = [
            self._update_for_gather(old_file, new_file, json_file)
            for old_file, (new_file, json_file) in file_mapping.items()
        ]
        await asyncio.gather(*tasks)
        await self._integrate_json()
        logger.info(f"##### {self._mention_name}字典更新完毕 !\n")

    async def _update_for_gather(self, old_file: Path, new_file: Path, json_file: Path):
        if not new_file.exists():  # 旧文件在新版本中删除/改名了
            unavailable_file = (
                DIR_RAW_DICTS
                / self._type
                / self._version
                / "csv"
                / "game"
                / "移除文件"
                / Path().joinpath(*old_file.parts[old_file.parts.index("utf8") + 1 :])
            )
            os.makedirs(unavailable_file.parent, exist_ok=True)
            with open(old_file, "r", encoding="utf-8") as fp:
                unavailables = list(csv.reader(fp))
            with open(unavailable_file, "w", encoding="utf-8-sig", newline="") as fp:
                csv.writer(fp).writerows(unavailables)
            return

        with open(old_file, "r", encoding="utf-8") as fp:
            old_data = list(csv.reader(fp))
            old_ens: dict = {
                row[-2] if len(row) > 2 else row[1]: idx_
                for idx_, row in enumerate(old_data)
            }  # 旧英文: 旧英文行键

        with open(new_file, "r", encoding="utf-8") as fp:
            new_data = list(csv.reader(fp))
            new_ens: dict = {
                row[-1]: idx_ for idx_, row in enumerate(new_data)
            }  # 字典英文: 旧英文行键

        with open(json_file, "r", encoding="utf-8") as fp:
            json_data: list[dict] = json.load(fp)

        # 1. 未变的键和汉化直接替换
        for idx_, row in enumerate(new_data):
            if row[-1] in old_ens:
                new_data[idx_][0] = old_data[old_ens[row[-1]]][0]
                if len(old_data[old_ens[row[-1]]]) >= 3:
                    ts = old_data[old_ens[row[-1]]][-1].strip()
                    new_data[idx_].append(ts)
                    try:
                        json_data[idx_]["translation"] = ts
                    except IndexError as e:
                        logger.error(f"json与csv长度不同: {json_file}")

        # 2. 不存在的英文移入失效词条
        unavailables = []
        for idx_, row in enumerate(old_data):
            if len(row) <= 2:  # 没翻译的，丢掉！
                continue
            if row[-2] == row[-1]:  # 不用翻译的，丢掉！
                continue

            old_en = row[-2]
            if old_en not in new_ens:
                unavailables.append(old_data[idx_])
        unavailable_file = (
            DIR_RAW_DICTS
            / self._type
            / self._version
            / "csv"
            / "game"
            / "失效词条"
            / Path().joinpath(*old_file.parts[old_file.parts.index("utf8") + 1 :])
            if unavailables
            else None
        )
        with open(old_file, "w", encoding="utf-8-sig", newline="") as fp:
            csv.writer(fp).writerows(old_data)

        with open(new_file, "w", encoding="utf-8-sig", newline="") as fp:
            csv.writer(fp).writerows(new_data)

        with open(new_file, "r", encoding="utf-8-sig") as fp:
            problem_data = fp.readlines()

        with open(json_file, "w", encoding="utf-8") as fp:
            json.dump(json_data, fp, ensure_ascii=False, indent=2)

        for idx, line in enumerate(problem_data):
            if "﻿" in line:
                problem_data[idx] = line.replace("﻿", "")

        with open(new_file, "w", encoding="utf-8-sig") as fp:
            fp.writelines(problem_data)

        if unavailable_file:
            os.makedirs(unavailable_file.parent, exist_ok=True)
            with open(unavailable_file, "w", encoding="utf-8-sig", newline="") as fp:
                csv.writer(fp).writerows(unavailables)

    async def _integrate_json(self):
        """把 json 字典合并成一个大的"""
        integrated_dict = []
        for root, dir_list, file_list in os.walk(
            DIR_RAW_DICTS / self._type / self._version / "json" / "game"
        ):
            for file in file_list:
                with open(Path(root) / file, "r", encoding="utf-8") as fp:
                    json_data: list[dict] = json.load(fp)

                json_data = [
                    item
                    for item in json_data
                    if item["original"] != item["translation"] and item["translation"]
                ]
                integrated_dict.extend(json_data)
        i18n_dict = await self._wash_json(integrated_dict)
        with open(DIR_DATA_ROOT / "json" / "i18n.json", "w", encoding="utf-8") as fp:
            json.dump(i18n_dict, fp, ensure_ascii=False, indent=2)

    @staticmethod
    async def _wash_json(integrated_dict: list[dict]) -> dict:
        """处理为 i18n mod 可接受的格式"""
        i18n_dict = {"typeB": {"TypeBOutputText": [], "TypeBInputStoryScript": []}}
        for data in integrated_dict:
            result_data = {
                "f": data["original"],
                "t": data["translation"],
                "pos": data["pos"],
            }

            filename = Path(data["filepath"]).name
            result_data["fileName"] = filename
            if filename.endswith(".js"):
                result_data["js"] = True
            elif filename.endswith(".css"):
                result_data["css"] = True

            if data["passage"]:
                result_data["pN"] = data["passage"]
                i18n_dict["typeB"]["TypeBInputStoryScript"].append(result_data)
                continue
            i18n_dict["typeB"]["TypeBOutputText"].append(result_data)
        return i18n_dict

    """ 替换游戏原文 """

    async def apply_dicts(
        self,
        blacklist_dirs: list[str] = None,
        blacklist_files: list[str] = None,
        debug_flag: bool = False,
        type_manual: str = None,
    ):
        """汉化覆写游戏文件"""
        if not self._version:
            await self.fetch_latest_version()

        if self._type == "common":
            DIR_GAME_TEXTS = DIR_GAME_TEXTS_COMMON
        else:
            DIR_GAME_TEXTS = DIR_GAME_TEXTS_DEV
        logger.info(f"===== 开始覆写{self._mention_name}汉化 ...")

        type_manual = type_manual or self._type
        if type_manual != self._type:
            os.makedirs(
                DIR_RAW_DICTS / "common" / self._version / "csv" / "game", exist_ok=True
            )
            for tree in os.listdir(DIR_PARATRANZ / "common" / "utf8"):
                with contextlib.suppress(shutil.Error, FileNotFoundError):
                    shutil.move(
                        DIR_PARATRANZ / "common" / "utf8" / tree,
                        DIR_RAW_DICTS / "common" / self._version / "csv" / "game",
                    )

        file_mapping: dict = {}
        for root, dir_list, file_list in os.walk(
            DIR_RAW_DICTS / type_manual / self._version / "csv"
        ):
            if any(_ in Path(root).absolute().__str__() for _ in blacklist_dirs):
                continue
            if "失效词条" in root or "移除文件" in root:
                continue
            for file in file_list:
                if any(_ in file for _ in blacklist_files):
                    continue
                if file.endswith(".js.csv"):
                    file_mapping[Path(root).absolute() / file] = (
                        DIR_GAME_TEXTS
                        / Path(root).relative_to(
                            DIR_RAW_DICTS / type_manual / self._version / "csv" / "game"
                        )
                        / f"{file.split('.')[0]}.js".replace("utf8\\", "")
                    )
                else:
                    file_mapping[Path(root).absolute() / file] = (
                        DIR_GAME_TEXTS
                        / Path(root).relative_to(
                            DIR_RAW_DICTS / type_manual / self._version / "csv" / "game"
                        )
                        / f"{file.split('.')[0]}.twee".replace("utf8\\", "")
                    )

        tasks = [
            self._apply_for_gather(csv_file, twee_file, debug_flag=debug_flag)
            for idx, (csv_file, twee_file) in enumerate(file_mapping.items())
        ]
        await asyncio.gather(*tasks)
        logger.info(f"##### {self._mention_name}汉化覆写完毕 !\n")

    async def _apply_for_gather(
        self, csv_file: Path, target_file: Path, debug_flag: bool = False
    ):
        with open(target_file, "r", encoding="utf-8") as fp:
            raw_targets: list[str] = fp.readlines()
        raw_targets_temp = raw_targets.copy()

        with open(csv_file, "r", encoding="utf-8") as fp:
            for row in csv.reader(fp):
                if len(row) < 3:  # 没汉化
                    continue
                en, zh = row[-2:]
                en, zh = en.strip(), zh.strip()
                if not zh:  # 没汉化/汉化为空
                    continue

                zh = re.sub("^(“)", '"', zh)
                zh = re.sub("(”)$", '"', zh)
                if self._is_lack_angle(zh, en):
                    logger.warning(
                        f"\t!!! 可能的尖括号数量错误：{en} | {zh} | https://paratranz.cn/projects/{PARATRANZ_PROJECT_DOL_ID}/strings?text={quote(en)}"
                    )
                    if debug_flag:
                        webbrowser.open(
                            f"https://paratranz.cn/projects/{PARATRANZ_PROJECT_DOL_ID}/strings?text={quote(en)}"
                        )
                if self._is_different_event(zh, en):
                    logger.warning(
                        f"\t!!! 可能的事件名称错翻：{en} | {zh} | https://paratranz.cn/projects/{PARATRANZ_PROJECT_DOL_ID}/strings?text={quote(en)}"
                    )
                    if debug_flag:
                        webbrowser.open(
                            f"https://paratranz.cn/projects/{PARATRANZ_PROJECT_DOL_ID}/strings?text={quote(en)}"
                        )

                for idx_, target_row in enumerate(raw_targets_temp):
                    if not target_row.strip():
                        continue

                    if en == target_row.strip():
                        raw_targets[idx_] = (
                            target_row.replace(en, zh).replace(" \n", "\n").lstrip(" ")
                        )
                        raw_targets_temp[idx_] = ""

        if target_file.name.endswith(".js"):
            try:
                self._acorn.parse("".join(raw_targets))
                LOGGER_COLOR.info(f"<g>JS 语法检测通过</g> {target_file}")
            except JSSyntaxError as err:
                try:
                    LOGGER_COLOR.error(f"{target_file} | {err.err_code(raw_targets)}")
                except ValueError as e:
                    LOGGER_COLOR.error(f"{target_file}")
        with open(target_file, "w", encoding="utf-8") as fp:
            fp.writelines(raw_targets)

    @staticmethod
    def _is_lack_angle(line_zh: str, line_en: str):
        if ("<" not in line_en and ">" not in line_en) or ParseTextTwee.is_only_marks(
            line_en
        ):
            return False

        if line_zh[0] == "<":
            line_zh = f"_{line_zh}"
        if line_en[0] == "<":
            line_en = f"_{line_en}"
        if line_zh[-1] == ">":
            line_zh = f"{line_zh}_"
        if line_en[-1] == ">":
            line_en = f"{line_en}_"

        left_angle_single_zh = re.findall(r"[^<=](<)[^<=3]", line_zh)
        right_angle_single_zh = re.findall(r"[^>=](>)[^>=:]", line_zh)
        if "<<" not in line_en and ">>" not in line_en:
            if len(left_angle_single_zh) == len(right_angle_single_zh):
                return False
            left_angle_single_en = re.findall(r"[^<=](<)[^<=3]", line_en)
            right_angle_single_en = re.findall(r"[^>=](>)[^>=:]", line_en)
            return len(left_angle_single_en) != len(left_angle_single_zh) or len(
                right_angle_single_en
            ) != len(right_angle_single_zh)

        left_angle_double_zh = re.findall(r"(<<)", line_zh)
        right_angle_double_zh = re.findall(r"(>>)", line_zh)
        if len(left_angle_double_zh) == len(right_angle_double_zh):
            return False

        left_angle_double_en = re.findall(r"(<<)", line_en)
        right_angle_double_en = re.findall(r"(>>)", line_en)
        return len(left_angle_double_en) != len(left_angle_double_zh) or len(
            right_angle_double_en
        ) != len(right_angle_double_zh)

    @staticmethod
    def _is_lack_square(line_zh: str, line_en: str):
        if "[" not in line_en and "]" not in line_en:
            return False

        if line_zh[0] == "[":
            line_zh = f"_{line_zh}"
        if line_en[0] == "[":
            line_en = f"_{line_en}"
        if line_zh[-1] == "]":
            line_zh = f"{line_zh}_"
        if line_en[-1] == "]":
            line_en = f"{line_en}_"

        left_square_single_zh = re.findall(r"[^\[](\[)[^\[]", line_zh)
        right_square_single_zh = re.findall(r"[^]](])[^]]", line_zh)
        if "[[" not in line_en and "]]" not in line_en:
            if len(left_square_single_zh) == len(right_square_single_zh):
                return False
            left_square_single_en = re.findall(r"[^\[](\[)[^\[]", line_en)
            right_square_single_en = re.findall(r"[^]](])[^]]", line_en)
            return len(left_square_single_en) != len(left_square_single_zh) or len(
                right_square_single_en
            ) != len(right_square_single_zh)

        left_square_double_zh = re.findall(r"(\[\[)", line_zh)
        right_square_double_zh = re.findall(r"(]])", line_zh)
        if len(left_square_double_zh) == len(right_square_double_zh):
            return False
        left_square_double_en = re.findall(r"(\[\[)", line_en)
        right_square_double_en = re.findall(r"(]])", line_en)
        return len(left_square_double_en) != len(left_square_double_zh) or len(
            right_square_double_en
        ) != len(right_square_double_zh)

    @staticmethod
    def _is_different_event(line_zh: str, line_en: str):
        if "<<link [[" not in line_en or not line_zh:
            return False
        event_en = re.findall(r"<<link\s\[\[.*?\|(.*?)\]\]", line_en)
        if not event_en:
            return False
        event_zh = re.findall(r"<<link\s\[\[.*?\|(.*?)\]\]", line_zh)
        return event_en != event_zh

    @staticmethod
    def _is_full_notation_new(line_zh: str, line_en: str):
        if "cn_name" in line_zh or "writ_cn" in line_zh:
            return False
        left_angle_double_en = re.findall(r'(",)', line_en)
        left_angle_double_zh = re.findall(r'(",)', line_zh)
        return len(left_angle_double_en) != len(left_angle_double_zh)

    @staticmethod
    def _is_lack_yin(line_zh: str, line_en: str):
        right_angle_double_en = re.findall(r'(")', line_en)
        right_angle_double_zh = re.findall(r'(")', line_zh)
        return (len(right_angle_double_en) - len(right_angle_double_zh)) % 2 != 0

    @staticmethod
    def _is_full_comma(line: str):
        return line.endswith('"，')

    @staticmethod
    def _is_full_notation(line_zh: str, line_en: str):
        if '",' in line_en and "”," in line_zh:
            return True
        return ': "' in line_en and ": “" in line_zh

    @staticmethod
    def _is_lost_notation(line_zh: str, line_en: str):
        if (
            any(line_en.endswith(_) for _ in {"',", '",', "`,"})
            and line_zh[-2:] != line_en[-2:]
        ):
            return True
        return False

    async def get_lastest_commit(self) -> None:
        """利用本地 Git 仓库获取最新的 commit 信息（替换原有的 httpx 请求，解决 403 拦截）"""
        if not self.DIR_GIT_REPO.exists():
            logger.error("本地 Git 仓库尚未初始化，无法获取 commit！")
            return None

        # 1. 使用 git log 获取本地最新分支的 commit hash
        res_id = subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            cwd=str(self.DIR_GIT_REPO),
            stdout=subprocess.PIPE,
            text=True,
        )
        # 2. 获取最新的 commit 提交日志信息
        res_msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=str(self.DIR_GIT_REPO),
            stdout=subprocess.PIPE,
            text=True,
        )

        latest_id = res_id.stdout.strip()
        if not latest_id:
            logger.error("无法从本地 Git 读取最新 commit hash")
            return None

        logger.info(f"latest commit: {latest_id}")
        self._is_latest = bool(self._commit and latest_id == self._commit["id"])
        if self._is_latest:
            return None

        logger.info(f"===== 开始写入{self._mention_name}最新 commit ...")

        latest_commit = {
            "id": latest_id,
            "message": res_msg.stdout.strip(),
            "short_id": latest_id[:8],
        }

        with open(FILE_COMMITS, "w", encoding="utf-8") as fp:
            json.dump(latest_commit, fp, ensure_ascii=False, indent=2)
            logger.info(f"#### {self._mention_name}最新 commit 已写入！")

    def get_type(self, common, dev):
        if self._type == "common":
            return common
        else:
            return dev

    @property
    def game_dir(self) -> Path:
        """获得 game 目录"""
        return self.get_type(DIR_GAME_ROOT_COMMON, DIR_GAME_ROOT_DEV)

    """其他要修改的东西"""

    def change_css(self):
        css_dir = DIR_GAME_CSS_COMMON if self._type == "common" else DIR_GAME_CSS_DEV
        with open(css_dir / "base.css", "r", encoding="utf-8") as fp:
            lines = fp.readlines()
        for idx, line in enumerate(lines):
            match line.strip():
                case "max-height: 2.4em;":
                    lines[idx] = line.replace("2.4em;", "7em;")
                    continue
                case 'content: " months";':
                    lines[idx] = line.replace(" months", "月数")
                    continue
                case 'content: " weeks";':
                    lines[idx] = line.replace(" weeks", "周数")
                    break
                case _:
                    continue
        with open(css_dir / "base.css", "w", encoding="utf-8") as fp:
            fp.writelines(lines)

    def replace_banner(self):
        shutil.copyfile(
            DIR_DATA_ROOT / "img" / "banner.png",
            DIR_GAME_ROOT_COMMON / "img" / "misc" / "banner.png",
        )

    def change_version(self, version: str = ""):
        with open(FILE_VERSION_EDIT_COMMON, "r", encoding="utf-8") as fp:
            lines = fp.readlines()
        for idx, line in enumerate(lines):
            if "versionName: " in line.strip():
                lines[idx] = f'versionName: "{version}",\n'
                break
        with open(FILE_VERSION_EDIT_COMMON, "w", encoding="utf-8") as fp:
            fp.writelines(lines)

    """ 删删删 """

    async def drop_all_dirs(self, force=False):
        if not force:
            await self.get_lastest_commit()
        logger.warning("===== 开始删库跑路 ...")
        await self._drop_temp()
        await self._drop_gitgud()
        await self._drop_dict()
        await self._drop_paratranz()
        logger.warning("##### 删库跑路完毕 !\n")

    async def _drop_temp(self):
        if DIR_TEMP_ROOT.exists():
            if not self._is_latest:
                shutil.rmtree(DIR_TEMP_ROOT, ignore_errors=True)
                return
            if FILE_REPOSITORY_ZIP.exists():
                with contextlib.suppress(shutil.Error, FileNotFoundError):
                    shutil.move(FILE_REPOSITORY_ZIP, DIR_ROOT)

            if (DIR_TEMP_ROOT / f"dol{self._mention_name}.zip").exists():
                with contextlib.suppress(shutil.Error, FileNotFoundError):
                    shutil.move(
                        DIR_TEMP_ROOT / f"dol{self._mention_name}.zip", DIR_ROOT
                    )

            if (DIR_TEMP_ROOT / "dol世扩.zip").exists():
                with contextlib.suppress(shutil.Error, FileNotFoundError):
                    shutil.move(DIR_TEMP_ROOT / "dol世扩.zip", DIR_ROOT)

            shutil.rmtree(DIR_TEMP_ROOT, ignore_errors=True)
        logger.warning("\t- 缓存目录已删除")

    async def _drop_gitgud(self):
        shutil.rmtree(self.game_dir, ignore_errors=True)
        logger.warning(f"\t- {self._mention_name}游戏目录已删除")

    async def _drop_dict(self):
        if not self._version:
            await self.fetch_latest_version()
        shutil.rmtree(DIR_RAW_DICTS / self._type / self._version, ignore_errors=True)
        shutil.rmtree(DIR_RAW_DICTS / "common" / self._version, ignore_errors=True)
        logger.warning(f"\t- {self._mention_name}字典目录已删除")

    async def _drop_paratranz(self):
        shutil.rmtree(DIR_PARATRANZ / self._type, ignore_errors=True)
        logger.warning(f"\t- {self._mention_name}汉化目录已删除")

    """ 编译游戏 """

    def compile(self, chs_version: str = ""):
        logger.info("===== 开始编译游戏 ...")
        if platform.system() == "Windows":
            self._compile_for_windows()
        elif platform.system() == "Linux":
            self._compile_for_linux()
        else:
            raise Exception("什么电脑系统啊？")
        logger.info("##### 游戏编译完毕 !")

    def _before_compile(self, chs_version: str = ""):
        # 1. 修复 Windows 的编译批处理
        if (self.game_dir / "compile.bat").exists():
            with open(self.game_dir / "compile.bat", "r", encoding="utf-8") as fp:
                content = fp.read()
            content = content.replace(
                "Degrees of Lewdity.html", "Degrees of Lewdity.html"
            )
            with open(self.game_dir / "compile.bat", "w", encoding="utf-8") as fp:
                fp.write(content)

        # 核心修复：如果是 Linux，强行把 compile.sh 里的输出文件名改成 Python 预期的固定名字
        if (self.game_dir / "compile.sh").exists():
            with open(self.game_dir / "compile.sh", "r", encoding="utf-8") as fp:
                content = fp.read()
            content = re.sub(
                r'-o\s+"Degrees of Lewdity[^"]*\.html"',
                '-o "Degrees of Lewdity.html"',
                content,
            )
            content = re.sub(
                r"-o\s+Degrees\s+of\s+Lewdity[^ \n]*\.html",
                '-o "Degrees of Lewdity.html"',
                content,
            )
            with open(self.game_dir / "compile.sh", "w", encoding="utf-8") as fp:
                fp.write(content)

        # 3. 后续原有的 Android 配置修改
        config_xml = (
            self.game_dir
            / "devTools"
            / "androidsdk"
            / "image"
            / "cordova"
            / "comfig.xml"
        )
        if config_xml.exists():
            with open(config_xml, "r", encoding="utf-8") as fp:
                lines = fp.readlines()
            for idx, line in enumerate(lines):
                if 'id="' in line:
                    lines[idx] = 'id="dol-chs"\n'
                    continue
                if 'version="' in line:
                    lines[idx] = f'version="{chs_version}"\n'
                    continue
                if 'android-packageName="' in line:
                    lines[idx] = (
                        'android-packageName="com.vrelnir.DegreesOfLewdityCHS"\n'
                    )
                    continue
                if "<description>Degrees of Lewdity</description>" in line:
                    lines[idx] = (
                        "<description>Degrees of Lewdity 汉化版</description>\n"
                    )
            with open(config_xml, "w", encoding="utf-8") as fp:
                fp.writelines(lines)

    def _compile_for_windows(self):
        subprocess.Popen(self.game_dir / "compile.bat")
        time.sleep(5)
        logger.info(
            f"\t- Windows 游戏编译完成，位于 {self.game_dir / 'Degrees of Lewdity.html'}"
        )

    def _compile_for_linux(self):
        if GITHUB_ACTION_DEV:
            tweego_exe = (
                "tweego_linux86"
                if PLATFORM_ARCHITECTURE == "32bit"
                else "tweego_linux64"
            )
            tweego_exe_file = self.game_dir / "devTools" / "tweego" / tweego_exe
            tweego_exe_file.chmod(tweego_exe_file.stat().st_mode | stat.S_IEXEC)
            tweego_compile_sh = self.game_dir / "compile.sh"
            tweego_compile_sh.chmod(tweego_compile_sh.stat().st_mode | stat.S_IEXEC)
        subprocess.Popen(
            "bash ./compile.sh", env=os.environ, shell=True, cwd=self.game_dir
        )
        time.sleep(5)
        logger.info(
            f"\t- Linux 游戏编译完成，位于 {self.game_dir / 'Degrees of Lewdity.html'}"
        )

    def _compile_for_mobile(self):
        """android"""

    """ 打包游戏 """

    def package_zip(self, chs_version: str = "chs"):
        today = datetime.datetime.now().strftime("%Y%m%d")
        with zf(
            DIR_GAME_ROOT_COMMON / f"dol-{chs_version}-{today}.zip",
            "w",
            compresslevel=9,
            compression=ZIP_DEFLATED,
        ) as zfp:
            for root, dir_list, file_list in os.walk(DIR_GAME_ROOT_COMMON):
                for file in file_list:
                    filepath = Path(
                        (Path(root) / file)
                        .__str__()
                        .split("degrees-of-lewdity-master/")[-1]
                        .split("degrees-of-lewdity-master\\")[-1]
                    )
                    if (
                        file
                        in {
                            "Degrees of Lewdity.html",
                            "style.css",
                        }
                        or "degrees-of-lewdity-master/img/" in root
                        or "degrees-of-lewdity-master\\img\\" in root
                        or filepath == Path("LICENSE")
                    ):
                        zfp.write(
                            filename=DIR_GAME_ROOT_COMMON / filepath,
                            arcname=filepath,
                            compresslevel=9,
                        )

    async def copy_to_git(self):
        git_repo = os.getenv("GIT_REPO")
        dol_chinese_path = DIR_ROOT / git_repo
        if not dol_chinese_path.exists():
            logger.warning(f"不存在{git_repo}文件夹")
            return

        logger.info("===== 开始复制到 git ...")
        game_dir_path = self.game_dir
        game_dir = os.listdir(game_dir_path)

        logger.info(f"game_dir: {game_dir}")
        for file in game_dir:
            if file.startswith("Degrees of Lewdity") and file.endswith("html"):
                dol_html = "beta" if GITHUB_ACTION_ISBETA else "index"
                game_html = game_dir_path / file
                logger.info("复制到GIT文件夹")
                shutil.copyfile(
                    game_html,
                    dol_chinese_path / f"{dol_html}.html",
                )
                beeesssmod_dir_path = dol_chinese_path / "beeesssmod"
                beeesssmod_dir = Path(beeesssmod_dir_path)
                if beeesssmod_dir.is_dir():
                    logger.info("同步到美化包文件夹")
                    shutil.copyfile(
                        game_html,
                        beeesssmod_dir_path / f"{dol_html}.html",
                    )

            elif file in {"style.css", "DolSettingsExport.json"}:
                logger.info(f"game_dir file: {file}")
                shutil.copyfile(
                    game_dir_path / file,
                    dol_chinese_path / file,
                )
        dol_chinese_img_path = dol_chinese_path / "img"

        shutil.copytree(
            self.game_dir / "img",
            dol_chinese_img_path,
            True,
            ignore=lambda src, files: [
                f for f in files if f.endswith(".js") or f.endswith(".bat")
            ],
            dirs_exist_ok=True,
        )
        logger.info("##### 复制到 git 已完毕! ")
        await self.drop_all_dirs(True)

    """ 在浏览器中启动 """

    def run(self):
        webbrowser.open((self.game_dir / "Degrees of Lewdity.html").__str__())

    """ i18n 相关"""

    async def download_modloader_autobuild(self):
        async with httpx.AsyncClient(verify=False) as client:
            await self._get_latest_modloader_autobuild(client)

    async def _get_latest_modloader_autobuild(self, client: httpx.AsyncClient):
        response = await client.get(REPOSITORY_MODLOADER_ARTIFACTS)
        url = response.json()["artifacts"][0]["archive_download_url"]

        logger.info(f"url: {url}")
        async with client.stream(
            "GET",
            url,
            headers={
                "accept": "application/vnd.github+json",
                "Authorization": f"Bearer {GITHUB_ACCESS_TOKEN}",
            },
            follow_redirects=True,
            timeout=60,
        ) as response:
            async with aopen(DIR_TEMP_ROOT / "modloader.zip", "wb+") as afp:
                async for char in response.iter_raw():
                    await afp.write(char)


__all__ = ["ProjectDOL"]
