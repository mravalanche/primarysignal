/* Small client-side filters for the fictional, fixed GitHub Pages tour. */
(() => {
  const listing = document.querySelector("[data-static-tour-listing]");
  if (!listing) return;

  const params = new URLSearchParams(location.search);
  const topicLinks = [...listing.querySelectorAll(".ps-latest-topic-link")];
  const form = listing.querySelector(".ps-latest-filters");
  const typeSelect = form?.querySelector('select[name="story_type"]');
  const ukCheckbox = form?.querySelector('input[name="uk_relevant"]');
  const topicInput = form?.querySelector('input[name="topic"]');
  const knownTopics = new Set(topicLinks.slice(1).map((link) => new URL(link.href).searchParams.get("topic")));
  const knownTypes = new Set([...typeSelect.options].map((option) => option.value));
  const topic = knownTopics.has(params.get("topic")) ? params.get("topic") : "";
  const storyType = knownTypes.has(params.get("story_type")) ? params.get("story_type") : "";
  const ukOnly = params.get("uk_relevant") === "true";

  topicLinks.forEach((link, index) => {
    const value = index === 0 ? "" : new URL(link.href).searchParams.get("topic");
    const target = new URL(link.href);
    target.search = "";
    if (value) target.searchParams.set("topic", value);
    if (storyType) target.searchParams.set("story_type", storyType);
    if (ukOnly) target.searchParams.set("uk_relevant", "true");
    link.href = target.href;
    link.classList.toggle("is-active", value === topic);
    if (value === topic) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });

  if (typeSelect) typeSelect.value = storyType;
  if (ukCheckbox) ukCheckbox.checked = ukOnly;
  if (form) {
    if (topic && !topicInput) {
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "topic";
      hidden.value = topic;
      form.append(hidden);
    } else if (topicInput) topicInput.value = topic;
  }

  let visible = 0;
  listing.querySelectorAll(".ps-latest-day").forEach((day) => {
    let dayCount = 0;
    day.querySelectorAll(".ps-story-row").forEach((story) => {
      const matches = (!topic || story.dataset.tourTopic === topic)
        && (!storyType || story.dataset.tourType === storyType)
        && (!ukOnly || story.dataset.tourUk === "true");
      story.style.display = matches ? "" : "none";
      if (matches) dayCount += 1;
    });
    day.style.display = dayCount ? "" : "none";
    const count = day.querySelector(".ps-latest-date > span");
    if (count) count.textContent = `${dayCount} ${dayCount === 1 ? "story" : "stories"}`;
    visible += dayCount;
  });
  const empty = listing.querySelector("[data-tour-empty]");
  if (empty) empty.style.display = visible ? "none" : "";
})();
