// SPDX-License-Identifier: GPL-2.0-or-later
/* Include the complete production interface translation unit; no TC stubs. */
#include QOSIFY_INTERFACE_SOURCE
#include <sys/stat.h>

static char resolved[IFNAMSIZ];
static int program_fds[4];

/* ASan retains the otherwise unused periodic timer through global metadata.
 * No ubus/bridger path is part of this test: fail if one is ever invoked. */
void qosify_ubus_update_bridger(bool shutdown)
{
	(void) shutdown;
	fputs("Unexpected bridger callback in interface lifecycle test\n", stderr);
	abort();
}

int qosify_ubus_check_interface(const char *name, char *ifname, int len)
{
	if (strcmp(name, "wan") || !resolved[0])
		return -1;

	snprintf(ifname, len, "%s", resolved);
	return 0;
}

const char *qosify_get_program(uint32_t flags, int *fd)
{
	static const char *suffixes[] = { "eth_egress", "eth_ingress",
		"ip_egress", "ip_ingress" };

	if (flags >= 4)
		return NULL;
	*fd = program_fds[flags];
	return suffixes[flags];
}

static bool private_namespaces(void)
{
	const char *names[] = { "net", "mnt" };
	const char *vars[] = { "QOSIFY_PARENT_NETNS", "QOSIFY_PARENT_MNTNS" };
	char path[64], value[128];
	int i, n;

	if (geteuid())
		return false;
	for (i = 0; i < 2; i++) {
		const char *parent = getenv(vars[i]);
		snprintf(path, sizeof(path), "/proc/self/ns/%s", names[i]);
		n = readlink(path, value, sizeof(value) - 1);
		if (!parent || n < 0)
			return false;
		value[n] = 0;
		if (!strcmp(parent, value))
			return false;
	}
	return true;
}

int main(int argc, char **argv)
{
	struct qosify_iface iface = {};
	char line[64], path[4096];
	unsigned i;

	if (!private_namespaces()) {
		fprintf(stderr, "Refusing interface test without private root network and mount namespaces\n");
		return 2;
	}
	if (argc != 2)
		return 2;
	for (i = 0; i < 4; i++) {
		snprintf(path, sizeof(path), "%s/prog%u", argv[1], i);
		program_fds[i] = bpf_obj_get(path);
		if (program_fds[i] < 0) {
			perror(path);
			return 2;
		}
	}
	ulog_open(ULOG_STDIO, LOG_DAEMON, "qosify-rebind-test");
	if (qosify_iface_init())
		return 2;
	iface.node.avl.key = "wan";
	iface.config.egress = true;
	iface.config.bandwidth_up = "100mbit";
	iface.config.mode = "diffserv4";
	setvbuf(stdout, NULL, _IOLBF, 0);
	puts("ready");
	while (fgets(line, sizeof(line), stdin)) {
		line[strcspn(line, "\n")] = 0;
		if (!strcmp(line, "quit"))
			break;
		if (!strcmp(line, "lost"))
			resolved[0] = 0;
		else if (strlen(line) < sizeof(resolved))
			memcpy(resolved, line, strlen(line) + 1);
		else
			return 2;
		qosify_iface_check_interface(&iface);
		printf("{\"ifname\":\"%s\",\"active\":%s}\n", iface.ifname,
			iface.active ? "true" : "false");
	}
	interface_stop(&iface);
	nl_socket_free(rtnl_sock);
	close(socket_fd);
	for (i = 0; i < 4; i++)
		close(program_fds[i]);
	return 0;
}
