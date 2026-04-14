"""AI Recruiter Agent - CLI 入口

用法:
    python -m recruiter.main collect              # 采集候选人
    python -m recruiter.main match <job_id>       # AI 匹配
    python -m recruiter.main generate <job_id>    # 生成消息
    python -m recruiter.main send                 # 发送已审核消息
    python -m recruiter.main replies              # 检测候选人回复
    python -m recruiter.main run <job_id>         # 全流程
    python -m recruiter.main scheduler <job_id>   # 定时调度
    python -m recruiter.main status               # 查看状态
"""

import argparse
import logging
import sys

from recruiter import config
from recruiter.db.models import Database
from recruiter.pipeline import RecruiterPipeline


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_collect(args):
    pipeline = RecruiterPipeline()
    stats = pipeline.collect(args.url)
    print(f"采集完成: 获取 {stats['total']} 人, 新增 {stats['new']} 人")


def cmd_resumes(args):
    pipeline = RecruiterPipeline()
    stats = pipeline.collect_resumes(args.limit)
    print(f"简历采集: {stats['collected']}/{stats['total']} 成功, {stats['failed']} 失败")


def cmd_match(args):
    pipeline = RecruiterPipeline()
    stats = pipeline.match(args.job_id, args.min_score)
    print(f"匹配完成: {stats['matched']} 人, 达标 {stats['qualified']} 人 (阈值={stats['threshold']})")


def cmd_generate(args):
    pipeline = RecruiterPipeline()
    stats = pipeline.generate_messages(args.job_id, args.min_score)
    print(f"消息生成: {stats['generated']} 条, 跳过 {stats['skipped']} 条")


def cmd_send(args):
    pipeline = RecruiterPipeline()
    stats = pipeline.send()
    print(f"发送结果: sent={stats['sent']}, failed={stats['failed']}, "
          f"timeout={stats['timeout']}, skipped={stats['skipped']}")
    if stats.get("reason"):
        print(f"原因: {stats['reason']}")


def cmd_replies(args):
    pipeline = RecruiterPipeline()
    stats = pipeline.check_replies()
    print(f"回复检测: 检查 {stats['checked']} 条, 发现回复 {stats['replied']} 条")


def cmd_scheduler(args):
    from recruiter.scheduler import run_scheduler
    print(f"启动定时调度 (job_id={args.job_id})")
    print(f"  采集间隔: {args.collect_interval} 分钟")
    print(f"  回复检测: {args.reply_interval} 分钟")
    print(f"  发送间隔: {args.send_interval} 分钟")
    print("按 Ctrl+C 停止\n")
    run_scheduler(
        job_id=args.job_id,
        collect_interval=args.collect_interval,
        reply_interval=args.reply_interval,
        send_interval=args.send_interval,
    )


def cmd_run(args):
    pipeline = RecruiterPipeline()
    stats = pipeline.run(
        job_id=args.job_id,
        job_url=args.url,
        auto_approve=args.auto_approve,
    )
    print("\n=== Pipeline 汇总 ===")
    for stage, s in stats.items():
        print(f"  {stage}: {s}")


def cmd_status(args):
    db = Database(config.DB_PATH)
    jobs = db.list_jobs()
    candidates = db.list_candidates(limit=99999)
    convs = db.list_conversations(limit=99999)

    print(f"数据库: {config.DB_PATH}")
    print(f"职位数: {len(jobs)}")
    print(f"候选人: {len(candidates)}")
    print(f"对话数: {len(convs)}")

    # 按状态统计对话
    status_counts = {}
    for c in convs:
        s = c["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
    if status_counts:
        print("对话状态:")
        for s, count in sorted(status_counts.items()):
            print(f"  {s}: {count}")

    print(f"\n浏览器驱动: {config.BROWSER_DRIVER}")
    print(f"AdsPower Profile: {config.ADSPOWER_PROFILE_ID or '(未配置)'}")
    db.close()


def main():
    parser = argparse.ArgumentParser(description="AI Recruiter Agent")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    sub = parser.add_subparsers(dest="command", help="子命令")

    # collect
    p_collect = sub.add_parser("collect", help="采集候选人")
    p_collect.add_argument("--url", default=None, help="采集页面 URL")
    p_collect.set_defaults(func=cmd_collect)

    # resumes
    p_resumes = sub.add_parser("resumes", help="采集候选人简历详情")
    p_resumes.add_argument("--limit", type=int, default=50, help="最多采集数量")
    p_resumes.set_defaults(func=cmd_resumes)

    # match
    p_match = sub.add_parser("match", help="AI 简历匹配")
    p_match.add_argument("job_id", type=int, help="职位 ID")
    p_match.add_argument("--min-score", type=int, default=None, help="最低分数阈值")
    p_match.set_defaults(func=cmd_match)

    # generate
    p_gen = sub.add_parser("generate", help="生成招呼消息")
    p_gen.add_argument("job_id", type=int, help="职位 ID")
    p_gen.add_argument("--min-score", type=int, default=None, help="最低分数阈值")
    p_gen.set_defaults(func=cmd_generate)

    # send
    p_send = sub.add_parser("send", help="发送已审核消息")
    p_send.set_defaults(func=cmd_send)

    # replies
    p_replies = sub.add_parser("replies", help="检测候选人回复")
    p_replies.set_defaults(func=cmd_replies)

    # scheduler
    p_sched = sub.add_parser("scheduler", help="定时调度")
    p_sched.add_argument("job_id", type=int, help="职位 ID")
    p_sched.add_argument("--collect-interval", type=int, default=60, help="采集间隔（分钟）")
    p_sched.add_argument("--reply-interval", type=int, default=10, help="回复检测间隔（分钟）")
    p_sched.add_argument("--send-interval", type=int, default=30, help="发送间隔（分钟）")
    p_sched.set_defaults(func=cmd_scheduler)

    # run
    p_run = sub.add_parser("run", help="全流程执行")
    p_run.add_argument("job_id", type=int, help="职位 ID")
    p_run.add_argument("--url", default=None, help="采集页面 URL")
    p_run.add_argument("--auto-approve", action="store_true", help="自动审核通过")
    p_run.set_defaults(func=cmd_run)

    # status
    p_status = sub.add_parser("status", help="查看系统状态")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
