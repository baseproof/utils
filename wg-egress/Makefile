# Build targets for the WireGuard egress fleet.
ZONE ?= us-central1-a
VM   ?= hub-ip

.PHONY: all hubd hubfind test clean deploy-hub hub-up

all: hubd hubfind

## hubd — control plane, runs on the GCP hub (linux/amd64)
hubd:
	cd hub && GOOS=linux GOARCH=amd64 go build -o ../build/hubd .

## hubfind — discovery agent, runs on each Mac (both architectures)
hubfind:
	cd client/hubfind && GOOS=darwin GOARCH=arm64 go build -o ../../build/hubfind-arm64 .
	cd client/hubfind && GOOS=darwin GOARCH=amd64 go build -o ../../build/hubfind-amd64 .

test:
	cd client/hubfind && go test -v ./...

deploy-hub: hubd
	gcloud compute scp build/hubd $(VM):~ --zone=$(ZONE) --tunnel-through-iap
	gcloud compute scp hub/hubd.service $(VM):~ --zone=$(ZONE) --tunnel-through-iap
	gcloud compute ssh $(VM) --zone=$(ZONE) --tunnel-through-iap --command '\
		sudo install -m755 ~/hubd /usr/local/bin/hubd && \
		sudo install -m644 ~/hubd.service /etc/systemd/system/hubd.service && \
		sudo mkdir -p /var/lib/hubd /etc/hubd && sudo chmod 700 /etc/hubd && \
		sudo systemctl daemon-reload && \
		sudo /usr/local/bin/hubd enroll-key && \
		sudo systemctl enable --now hubd && sleep 3 && systemctl is-active hubd'

clean:
	rm -rf build

## hub-up — pull the repo on the VM and converge it with ansible
hub-up:
	gcloud compute ssh $(VM) --zone=$(ZONE) --tunnel-through-iap --ssh-flag=-A --command 'set -e; \
		if [ -d ~/utils/.git ]; then git -C ~/utils pull --ff-only; \
		else git clone git@github.com:baseproof/utils.git ~/utils; fi; \
		sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ansible >/dev/null; \
		sudo ansible-playbook -i localhost, -c local \
			-e src_dir=$$HOME/utils/wg-egress/hub \
			$$HOME/utils/wg-egress/hub/ansible/playbook.yml'
